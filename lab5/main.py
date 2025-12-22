import csv
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import matplotlib
matplotlib.use("TkAgg")

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import serial  # type: ignore
from serial.tools import list_ports  # type: ignore

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ДЕФОЛТЫ
DEFAULT_PORT = "COM1"
DEFAULT_BAUD = 9600
DEFAULT_CSV = "data.csv"
DEFAULT_MAX_POINTS = 500
DEFAULT_UPDATE_MS = 10


def list_serial_ports() -> list[str]:
    ports = []
    for p in list_ports.comports():
        ports.append(p.device)
    return ports


def ensure_csv_header(csv_path: str) -> None:
    """
    Создаёт CSV с заголовком, если файла нет или он пустой.
    """
    p = Path(csv_path)
    if p.exists() and p.stat().st_size > 0:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "value"])


def parse_value_from_line(line: str) -> Optional[float]:
    s = line.strip()
    if not s:
        return None

    if ";" in s and "," not in s:
        parts = s.split(";")
    else:
        parts = s.split(",")

    raw = parts[-1].strip()

    try:
        return float(raw)
    except ValueError:
        return None


class SerialCSVSession:
    def __init__(self) -> None:
        self.ser: Optional[serial.Serial] = None
        self.csvfile = None
        self.csv_writer: Optional[csv.writer] = None

    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def connect(self, port: str, baud: int, timeout: float = 0.1) -> None:
        self.disconnect()
        self.ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(1.5)  # небольшая стабилизация

    def start_csv(self, csv_path: str) -> None:
        ensure_csv_header(csv_path)
        self.csvfile = open(csv_path, "a", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csvfile)

    def stop_csv(self) -> None:
        if self.csvfile and not self.csvfile.closed:
            self.csvfile.close()
        self.csvfile = None
        self.csv_writer = None

    def disconnect(self) -> None:
        self.stop_csv()
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def read_all_available_lines(self) -> list[str]:
        """
        Считывает всё, что уже накопилось во входном буфере.
        """
        if not self.is_connected():
            return []

        lines: list[str] = []
        assert self.ser is not None

        while self.ser.in_waiting > 0:
            try:
                b = self.ser.readline()
                if not b:
                    break
                s = b.decode("utf-8", errors="ignore").strip()
                if s:
                    lines.append(s)
            except Exception:
                # битые данные просто игнорируем
                continue
        return lines

    def write_row(self, timestamp: str, value: float) -> None:
        if self.csv_writer is None:
            return
        self.csv_writer.writerow([timestamp, value])
        if self.csvfile is not None:
            self.csvfile.flush()


class KGRApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("KGR / pySerial")
        self.root.geometry("1100x700")

        self.session = SerialCSVSession()
        self.running = False

        # буферы
        self.max_points = tk.IntVar(value=DEFAULT_MAX_POINTS)
        self.update_ms = tk.IntVar(value=DEFAULT_UPDATE_MS)

        self.times = deque(maxlen=self.max_points.get())
        self.values = deque(maxlen=self.max_points.get())

        # настройки подключения
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.baud_var = tk.IntVar(value=DEFAULT_BAUD)

        # файл
        self.csv_var = tk.StringVar(value=DEFAULT_CSV)

        # статистика
        self.last_value_var = tk.StringVar(value="—")
        self.samples_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Отключено")

        self._build_ui()
        self._refresh_ports()
        self._schedule_tick()
        # закрытие
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        # верхняя панель
        top = ttk.Frame(self.root, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        # Порт
        ttk.Label(top, text="Порт:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=12, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=(6, 12), sticky="w")

        ttk.Button(top, text="Обновить порты", command=self._refresh_ports).grid(row=0, column=2, padx=(0, 16))

        # Baud
        ttk.Label(top, text="Baudrate:").grid(row=0, column=3, sticky="w")
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=(6, 16), sticky="w")

        # CSV
        ttk.Label(top, text="CSV:").grid(row=0, column=5, sticky="w")
        ttk.Entry(top, textvariable=self.csv_var, width=32).grid(row=0, column=6, padx=(6, 6), sticky="w")
        ttk.Button(top, text="Выбрать…", command=self._choose_csv).grid(row=0, column=7, padx=(0, 16))

        # Параметры графика
        ttk.Label(top, text="Точек:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.max_points, width=10).grid(row=1, column=1, padx=(6, 12), sticky="w", pady=(8, 0))

        ttk.Label(top, text="Обновление (мс):").grid(row=1, column=3, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.update_ms, width=10).grid(row=1, column=4, padx=(6, 16), sticky="w", pady=(8, 0))

        # Кнопки управления
        self.start_btn = ttk.Button(top, text="Start", command=self.start)
        self.start_btn.grid(row=1, column=6, sticky="e", pady=(8, 0))
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=1, column=7, sticky="w", padx=(6, 0), pady=(8, 0))

        # инфо панель
        info = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        info.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(info, text="Статус:").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(6, 16))

        ttk.Label(info, text="Последнее значение:").grid(row=0, column=2, sticky="w")
        ttk.Label(info, textvariable=self.last_value_var).grid(row=0, column=3, sticky="w", padx=(6, 16))

        ttk.Label(info, text="Сэмплов:").grid(row=0, column=4, sticky="w")
        ttk.Label(info, textvariable=self.samples_var).grid(row=0, column=5, sticky="w", padx=(6, 0))

        # область графика
        plot_frame = ttk.Frame(self.root, padding=10)
        plot_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(10, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("Данные с датчика в реальном времени")
        self.ax.set_xlabel("Время (последние точки)")
        self.ax.set_ylabel("Значение")
        self.ax.grid(True)

        self.line, = self.ax.plot([], [], linewidth=1.5)

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill=tk.BOTH, expand=True)

    def _choose_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Выберите CSV файл",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=os.path.basename(self.csv_var.get()) or "data.csv",
        )
        if path:
            self.csv_var.set(path)

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        if not ports:
            ports = [DEFAULT_PORT]  # чтобы комбобокс не был пустым
        self.port_combo["values"] = ports
        # если текущий порт отсутствует — выберем первый
        if self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def _apply_max_points(self) -> None:
        """
        Если пользователь поменял max_points, пересоздаем deques с новым maxlen.
        """
        new_max = max(10, int(self.max_points.get()))
        if new_max == self.times.maxlen:
            return

        old_times = list(self.times)[-new_max:]
        old_values = list(self.values)[-new_max:]

        self.times = deque(old_times, maxlen=new_max)
        self.values = deque(old_values, maxlen=new_max)

    def start(self) -> None:
        if self.running:
            return

        port = self.port_var.get().strip()
        baud = int(self.baud_var.get())
        csv_path = self.csv_var.get().strip()

        if not port:
            messagebox.showerror("Ошибка", "Не выбран COM-порт.")
            return
        if not csv_path:
            messagebox.showerror("Ошибка", "Не задан путь к CSV.")
            return

        try:
            self._apply_max_points()
            self.session.connect(port, baud, timeout=0.1)
            self.session.start_csv(csv_path)

            self.running = True
            self.status_var.set(f"Подключено: {port} @ {baud}")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")

        except serial.SerialException as e:
            self.running = False
            self.session.disconnect()
            self.status_var.set("Ошибка подключения")
            messagebox.showerror("Serial ошибка", str(e))
        except Exception as e:
            self.running = False
            self.session.disconnect()
            self.status_var.set("Ошибка")
            messagebox.showerror("Ошибка", str(e))

    def stop(self) -> None:
        if not self.running and not self.session.is_connected():
            return

        self.running = False
        self.session.disconnect()

        self.status_var.set("Отключено")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def on_close(self) -> None:
        try:
            self.stop()
        finally:
            self.root.destroy()

    def _schedule_tick(self) -> None:
        ms = max(1, int(self.update_ms.get()))
        self.root.after(ms, self._tick)

    def _tick(self) -> None:
        try:
            if self.running and self.session.is_connected():
                lines = self.session.read_all_available_lines()
                if lines:
                    new_points = 0
                    for line in lines:
                        val = parse_value_from_line(line)
                        if val is None:
                            continue
                        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                        self.session.write_row(ts, val)

                        self.times.append(ts)
                        self.values.append(val)
                        new_points += 1

                    if new_points > 0:
                        self.last_value_var.set(str(self.values[-1]))
                        self.samples_var.set(str(len(self.values)))
                        self._redraw_plot()

        except Exception as e:
            self.running = False
            self.session.disconnect()
            self.status_var.set("Ошибка во время чтения")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            messagebox.showerror("Ошибка чтения", str(e))

        finally:
            self._schedule_tick()

    def _redraw_plot(self) -> None:
        if not self.values:
            return

        # X — просто индекс (быстрее), метки — время
        y = list(self.values)
        x = list(range(len(y)))
        self.line.set_data(x, y)

        self.ax.set_xlim(0, max(10, len(y) - 1))

        y_min = min(y) - 1
        y_max = max(y) + 1
        if y_min == y_max:
            y_min -= 1
            y_max += 1
        self.ax.set_ylim(y_min, y_max)

        # метки времени на оси X
        total = len(self.times)
        step = max(1, total // 10)
        ticks = list(range(0, total, step))
        labels = list(self.times)[::step]
        self.ax.set_xticks(ticks)
        self.ax.set_xticklabels(labels, rotation=45, ha="right")

        self.fig.tight_layout()
        self.canvas.draw_idle()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass

    app = KGRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
