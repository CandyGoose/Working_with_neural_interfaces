import inspect
if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec  # type: ignore

import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from serial.tools import list_ports  # type: ignore
from pyfirmata import Arduino, util  # type: ignore


VREF_CONST = 5.0

def list_serial_ports() -> list[str]:
    out = [p.device for p in list_ports.comports()]
    return out or ["COM1"]


def ensure_csv_header(path: str) -> None:
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "raw", "voltage"])


def now_hhmmss_ms() -> str:
    dt = datetime.now()
    ms = dt.microsecond // 1000
    return dt.strftime("%H:%M:%S") + f":{ms:03d}"


def parse_hhmmss_ms_to_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None

    # ISO
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass

    # HH:MM:SS:ms
    parts = s.split(":")
    try:
        if len(parts) == 4:
            hh, mm, ss, ms = parts
            us = int(ms) * 1000
            return datetime(date.today().year, date.today().month, date.today().day,
                            int(hh), int(mm), int(ss), us)
    except Exception:
        pass

    # HH:MM:SS(.ffffff)
    try:
        if "." in s:
            t = datetime.strptime(s, "%H:%M:%S.%f").time()
        else:
            t = datetime.strptime(s, "%H:%M:%S").time()
        return datetime.combine(date.today(), t)
    except Exception:
        return None


def load_csv_timeseries(path: str) -> Tuple[List[str], List[datetime], List[float]]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return [], [], []

    labels: List[str] = []
    times_dt: List[datetime] = []
    raws: List[float] = []

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return [], [], []
        fields = {x.strip() for x in reader.fieldnames if x}
        if "timestamp" not in fields or "raw" not in fields:
            return [], [], []

        for row in reader:
            try:
                ts = str(row["timestamp"]).strip()
                dt = parse_hhmmss_ms_to_dt(ts)
                if dt is None:
                    continue
                raw = float(str(row["raw"]).strip())
                labels.append(ts)
                times_dt.append(dt)
                raws.append(raw)
            except Exception:
                continue

    return labels, times_dt, raws


@dataclass
class FirmataSession:
    board: Optional[Arduino] = None
    iterator: Optional[util.Iterator] = None
    ain = None
    csvfile = None
    writer: Optional[csv.writer] = None

    def is_connected(self) -> bool:
        return self.board is not None

    def connect(self, port: str, analog_pin: int) -> None:
        self.disconnect()

        self.board = Arduino(port)
        self.iterator = util.Iterator(self.board)
        self.iterator.start()

        self.ain = self.board.get_pin(f"a:{analog_pin}:i")
        self.ain.enable_reporting()

        # прогрев
        t0 = time.time()
        while self.ain.read() is None and time.time() - t0 < 3.0:
            time.sleep(0.01)

    def start_csv(self, path: str) -> None:
        ensure_csv_header(path)
        self.csvfile = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csvfile)

    def write_row(self, ts_label: str, raw: int, voltage: float) -> None:
        if self.writer is None:
            return
        self.writer.writerow([ts_label, raw, f"{voltage:.6f}"])
        if self.csvfile is not None:
            self.csvfile.flush()

    def read_value_norm(self) -> Optional[float]:
        if self.ain is None:
            return None
        try:
            return self.ain.read()
        except Exception:
            return None

    def disconnect(self) -> None:
        if self.csvfile and not self.csvfile.closed:
            self.csvfile.close()
        self.csvfile = None
        self.writer = None

        if self.board is not None:
            try:
                self.board.exit()
            except Exception:
                pass
        self.board = None
        self.iterator = None
        self.ain = None


class FirmataUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Pulse Sensor Data from FIRMATA")
        self.root.geometry("1200x720")

        self.session = FirmataSession()
        self.running = False
        self.last_redraw: float = 0.0

        # данные
        self.labels: List[str] = [] # "HH:MM:SS:ms" для подписей оси X
        self.times_dt: List[datetime] = []  # для оценки Fs
        self.raws: List[float] = []

        # UI vars
        self.port_var = tk.StringVar(value="COM3")
        self.pin_var = tk.IntVar(value=0)
        self.target_hz_var = tk.DoubleVar(value=100.0)
        self.csv_var = tk.StringVar(value="data.csv")
        self.update_ms_var = tk.IntVar(value=10)

        # info
        self.status_var = tk.StringVar(value="Отключено")
        self.fs_var = tk.StringVar(value="—")
        self.last_raw_var = tk.StringVar(value="—")
        self.samples_var = tk.StringVar(value="0")

        self._build_ui()
        self._refresh_ports()
        self._schedule_tick()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Порт:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=12, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=(6, 10), sticky="w")
        ttk.Button(top, text="Обновить", command=self._refresh_ports).grid(row=0, column=2, padx=(0, 18))

        ttk.Label(top, text="Analog pin (A0=0):").grid(row=0, column=3, sticky="w")
        ttk.Entry(top, textvariable=self.pin_var, width=6).grid(row=0, column=4, padx=(6, 18), sticky="w")

        ttk.Label(top, text="target Hz:").grid(row=0, column=5, sticky="w")
        ttk.Entry(top, textvariable=self.target_hz_var, width=8).grid(row=0, column=6, padx=(6, 18), sticky="w")

        ttk.Label(top, text="tick (ms):").grid(row=0, column=7, sticky="w")
        ttk.Entry(top, textvariable=self.update_ms_var, width=6).grid(row=0, column=8, padx=(6, 18), sticky="w")

        ttk.Label(top, text="CSV:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.csv_var, width=45).grid(
            row=1, column=1, columnspan=5, padx=(6, 6), sticky="w", pady=(8, 0)
        )
        ttk.Button(top, text="Выбрать…", command=self._choose_csv).grid(row=1, column=6, padx=(0, 18), pady=(8, 0))

        self.start_btn = ttk.Button(top, text="Start", command=self.start)
        self.start_btn.grid(row=1, column=8, pady=(8, 0), sticky="e")
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=1, column=9, pady=(8, 0), sticky="w")

        info = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        info.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(info, text="Статус:").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(6, 16))

        ttk.Label(info, text="Fs (оценка):").grid(row=0, column=2, sticky="w")
        ttk.Label(info, textvariable=self.fs_var).grid(row=0, column=3, sticky="w", padx=(6, 16))

        ttk.Label(info, text="Последний raw:").grid(row=0, column=4, sticky="w")
        ttk.Label(info, textvariable=self.last_raw_var).grid(row=0, column=5, sticky="w", padx=(6, 16))

        ttk.Label(info, text="Сэмплов:").grid(row=0, column=6, sticky="w")
        ttk.Label(info, textvariable=self.samples_var).grid(row=0, column=7, sticky="w", padx=(6, 0))

        # plot
        plot_frame = ttk.Frame(self.root, padding=10)
        plot_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(11, 6), dpi=100)
        self.ax = self.fig.add_subplot(111)

        self.ax.set_title("Pulse Sensor Data from FIRMATA")
        self.ax.set_xlabel("Time (HH:MM:SS:ms)")
        self.ax.set_ylabel("Raw Value")
        self.ax.set_ylim(0, 1023)
        self.ax.grid(True)

        (self.line,) = self.ax.plot([], [], label="Raw Value (0-1023)", linewidth=1.5)
        self.ax.legend(loc="upper left")

        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def _choose_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Выберите CSV файл",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=os.path.basename(self.csv_var.get()) or "data.csv",
        )
        if path:
            self.csv_var.set(path)

    def start(self) -> None:
        if self.running:
            return

        port = self.port_var.get().strip()
        pin = int(self.pin_var.get())
        csv_path = self.csv_var.get().strip()

        if not port:
            messagebox.showerror("Ошибка", "Не выбран порт.")
            return

        try:
            self.session.connect(port, pin)
            if csv_path:
                self.session.start_csv(csv_path)

            self.running = True
            self.last_redraw = 0.0

            self.status_var.set(f"Подключено: {port}, A{pin}")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")

        except Exception as e:
            self.running = False
            self.session.disconnect()
            self.status_var.set("Ошибка подключения")
            messagebox.showerror("Ошибка", str(e))

    def stop(self) -> None:
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
        ms = max(1, int(self.update_ms_var.get()))
        self.root.after(ms, self._tick)

    def _tick(self) -> None:
        try:
            if self.running and self.session.is_connected():
                v = self.session.read_value_norm()
                if v is not None:
                    ts_label = now_hhmmss_ms()
                    ts_dt = parse_hhmmss_ms_to_dt(ts_label) or datetime.now()

                    raw = int(round(float(v) * 1023.0))
                    voltage = float(v) * VREF_CONST

                    self.labels.append(ts_label)
                    self.times_dt.append(ts_dt)
                    self.raws.append(float(raw))

                    self.samples_var.set(str(len(self.raws)))
                    self.last_raw_var.set(str(raw))

                    self.session.write_row(ts_label, raw, voltage)

                target_hz = float(self.target_hz_var.get())
                if target_hz > 0:
                    time.sleep(max(0.0, 1.0 / target_hz - 0.0005))

                now = time.time()
                if now - self.last_redraw > 0.1 and len(self.raws) > 2:
                    self._redraw()
                    self.last_redraw = now

        except Exception as e:
            self.running = False
            self.session.disconnect()
            self.status_var.set("Ошибка чтения")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            messagebox.showerror("Ошибка", str(e))
        finally:
            self._schedule_tick()

    def _estimate_fs(self) -> float:
        if len(self.times_dt) < 3:
            return float("nan")
        dt = (self.times_dt[-1] - self.times_dt[0]).total_seconds()
        if dt <= 0:
            return float("nan")
        return (len(self.times_dt) - 1) / dt

    def _redraw(self) -> None:
        if not self.raws:
            return

        x = list(range(len(self.raws)))
        self.line.set_data(x, self.raws)

        self.ax.set_xlim(0, max(10, len(self.raws) - 1))
        self.ax.set_ylim(0, 1023)

        # подписи времени как HH:MM:SS:ms
        total = len(self.labels)
        step = max(1, total // 10)
        ticks = list(range(0, total, step))
        tick_labels = self.labels[::step]

        self.ax.set_xticks(ticks)
        self.ax.set_xticklabels(tick_labels, rotation=45, ha="right")

        fs = self._estimate_fs()
        self.fs_var.set(f"{fs:.1f} Hz" if np.isfinite(fs) else "—")

        self.fig.tight_layout()
        self.canvas.draw_idle()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    FirmataUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
