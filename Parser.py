"""
MDGT-RUTUBE-SAVER - PyQt5 GUI for downloading Rutube videos using yt-dlp
Features:
 - 3 input modes: single link, batch (multi-line), auto-extract from rich text
 - Queue view with per-item status
 - Console window (logs)
 - Multithreaded downloads using QThreadPool (configurable workers)
 - Check / install yt-dlp from GUI
 - Reset (clear queue, clear logs) and optional deletion of downloaded files from current session
 - Theming (light/dark/blue)
 - Detailed instructions dialog

Note: requires Python 3.8+, PyQt5 and optionally yt-dlp. If yt-dlp isn't installed the GUI allows installing it (using the same Python interpreter).

Save this file and run:
    python MDGT-RUTUBE-SAVER.py

"""

import os
import re
import sys
import traceback
import shutil
from functools import partial
from pathlib import Path

from PyQt5.QtCore import (
    Qt,
    QRunnable,
    QObject,
    pyqtSignal,
    pyqtSlot,
    QThreadPool,
    QProcess,
)
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPlainTextEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QSplitter,
    QProgressBar,
    QMessageBox,
    QSpinBox,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QStyleFactory,
)

# Try import yt_dlp but don't crash the app if it's missing
try:
    from yt_dlp import YoutubeDL
    YT_DLP_AVAILABLE = True
except Exception:
    YoutubeDL = None
    YT_DLP_AVAILABLE = False


APP_NAME = "MDGT-RUTUBE-SAVER"
RUTUBE_LINK_RE = re.compile(
    r"https?://(?:www\.)?rutube\.ru/[^\s\"'>]+",
    re.IGNORECASE
)


class Signals(QObject):
    append_console = pyqtSignal(str)
    item_progress = pyqtSignal(str, int)  # url, progress percent
    item_status = pyqtSignal(str, str)  # url, status text
    item_finished = pyqtSignal(str, str)  # url, filepath
    enable_ui = pyqtSignal(bool)


class DownloadWorker(QRunnable):
    """QRunnable that downloads a single URL using yt_dlp and reports progress via signals"""

    def __init__(self, url: str, out_dir: str, ydl_opts: dict, signals: Signals):
        super().__init__()
        self.url = url
        self.out_dir = out_dir
        self.ydl_opts = dict(ydl_opts)
        self.signals = signals

    @pyqtSlot()
    def run(self):
        try:
            # embed progress hook
            def progress_hook(d):
                try:
                    status = d.get('status')
                    if status == 'downloading':
                        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                        downloaded = d.get('downloaded_bytes', 0)
                        percent = 0
                        if total:
                            percent = int(downloaded / total * 100)
                        text = d.get('eta')
                        self.signals.item_progress.emit(self.url, percent)
                        self.signals.append_console.emit(f"[{self.url}] downloading: {percent}% ETA={d.get('eta')}")
                    elif status == 'finished':
                        filename = d.get('filename') or d.get('info_dict', {}).get('filepath')
                        self.signals.item_progress.emit(self.url, 100)
                        self.signals.append_console.emit(f"[{self.url}] finished -> {filename}")
                        self.signals.item_finished.emit(self.url, filename or '')
                except Exception as e:
                    self.signals.append_console.emit(f"[hook error] {e}")

            self.ydl_opts.setdefault('outtmpl', os.path.join(self.out_dir, '%(title)s.%(ext)s'))
            self.ydl_opts['progress_hooks'] = [progress_hook]
            # Better logging into our console
            class YDLLogger:
                def __init__(self, signals):
                    # store reference to Signals instance so logger methods can emit to GUI
                    self.signals = signals

                def debug(self, msg):
                    # yt_dlp prints a lot of debug, ignore most
                    pass

                def info(self, msg):
                    try:
                        self.signals.append_console.emit(f"[yt-dlp] {msg}")
                    except Exception:
                        pass

                def warning(self, msg):
                    try:
                        self.signals.append_console.emit(f"[WARNING] {msg}")
                    except Exception:
                        pass

                def error(self, msg):
                    try:
                        self.signals.append_console.emit(f"[ERROR] {msg}")
                    except Exception:
                        pass

            # attach logger instance that has access to our signals
            self.ydl_opts.setdefault('logger', YDLLogger(self.signals))

            # perform download
            self.signals.append_console.emit(f"Starting download: {self.url}")
            if YoutubeDL is None:
                self.signals.append_console.emit("yt-dlp is not available. Skipping.")
                self.signals.item_status.emit(self.url, 'missing yt-dlp')
                return

            with YoutubeDL(self.ydl_opts) as ydl:
                result = ydl.download([self.url])
                # Note: progress hook will catch finished filename
                self.signals.item_status.emit(self.url, 'done')

        except Exception as e:
            tb = traceback.format_exc()
            self.signals.append_console.emit(f"Error downloading {self.url}: {e}\n{tb}")
            self.signals.item_status.emit(self.url, 'error')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 700)
        self.download_dir = os.path.abspath('downloads')
        os.makedirs(self.download_dir, exist_ok=True)

        self.threadpool = QThreadPool.globalInstance()
        self.signals = Signals()
        self.signals.append_console.connect(self._append_console)
        self.signals.item_progress.connect(self._on_item_progress)
        self.signals.item_status.connect(self._on_item_status)
        self.signals.item_finished.connect(self._on_item_finished)
        self.signals.enable_ui.connect(self._set_ui_enabled)

        # Keep track of queued urls and downloaded files
        self.queued_urls = []
        self.downloaded_files = []
        self.running_count = 0

        self._build_ui()
        self._apply_theme('Dark')
        self._refresh_yt_dlp_state()

    def _build_ui(self):
        main = QWidget()
        self.setCentralWidget(main)

        # Left: input controls
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)

        title = QLabel(APP_NAME)
        title.setFont(QFont('Segoe UI', 16, QFont.Bold))
        left_layout.addWidget(title)

        # Single link entry
        left_layout.addWidget(QLabel('1) Вставить одну ссылку:'))
        self.single_input = QLineEdit()
        self.single_input.setPlaceholderText('https://rutube.ru/video/...')
        left_layout.addWidget(self.single_input)

        h1 = QHBoxLayout()
        self.add_single_btn = QPushButton('Добавить в очередь')
        self.add_single_btn.clicked.connect(self.add_single)
        h1.addWidget(self.add_single_btn)

        left_layout.addLayout(h1)

        # Batch multiline
        left_layout.addWidget(QLabel('2) Вставить несколько ссылок (каждая с новой строки):'))
        self.batch_input = QPlainTextEdit()
        self.batch_input.setFixedHeight(90)
        left_layout.addWidget(self.batch_input)

        h2 = QHBoxLayout()
        self.add_batch_btn = QPushButton('Добавить все')
        self.add_batch_btn.clicked.connect(self.add_batch)
        h2.addWidget(self.add_batch_btn)
        left_layout.addLayout(h2)

        # Rich text automatic extractor
        left_layout.addWidget(QLabel('3) Вставить произвольный текст — кнопка извлечет все rutube-ссылки:'))
        self.rich_input = QTextEdit()
        self.rich_input.setFixedHeight(140)
        left_layout.addWidget(self.rich_input)

        h3 = QHBoxLayout()
        self.extract_btn = QPushButton('Извлечь ссылки')
        self.extract_btn.clicked.connect(self.extract_links)
        h3.addWidget(self.extract_btn)
        left_layout.addLayout(h3)

        # Options
        left_layout.addWidget(QLabel('Параметры скачивания:'))
        opts_layout = QHBoxLayout()
        opts_layout.addWidget(QLabel('Потоки:'))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setValue(3)
        opts_layout.addWidget(self.threads_spin)

        opts_layout.addWidget(QLabel('Папка:'))
        self.dir_btn = QPushButton(self.download_dir)
        self.dir_btn.clicked.connect(self.choose_dir)
        opts_layout.addWidget(self.dir_btn)
        left_layout.addLayout(opts_layout)

        # Buttons: Start, Stop, Reset
        ops = QHBoxLayout()
        self.start_btn = QPushButton('Старт')
        self.start_btn.clicked.connect(self.start_downloads)
        ops.addWidget(self.start_btn)

        self.stop_btn = QPushButton('Остановить')
        self.stop_btn.clicked.connect(self.stop_downloads)
        self.stop_btn.setEnabled(False)
        ops.addWidget(self.stop_btn)

        self.reset_btn = QPushButton('Сброс (очистить список)')
        self.reset_btn.clicked.connect(self.reset_all)
        ops.addWidget(self.reset_btn)
        left_layout.addLayout(ops)

        # yt-dlp check
        yt_layout = QHBoxLayout()
        self.yt_label = QLabel('yt-dlp: неизвестно')
        yt_layout.addWidget(self.yt_label)
        self.install_btn = QPushButton('Установить yt-dlp')
        self.install_btn.clicked.connect(self.install_yt_dlp)
        yt_layout.addWidget(self.install_btn)
        left_layout.addLayout(yt_layout)

        # Theme / instructions
        foot = QHBoxLayout()
        foot.addWidget(QLabel('Тема:'))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(['Dark', 'Light', 'Blue'])
        self.theme_combo.currentTextChanged.connect(self._apply_theme)
        foot.addWidget(self.theme_combo)

        self.instr_btn = QPushButton('Инструкция')
        self.instr_btn.clicked.connect(self.show_instructions)
        foot.addWidget(self.instr_btn)

        left_layout.addLayout(foot)

        left_layout.addStretch()

        # Right: queue and console
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)

        right_layout.addWidget(QLabel('Очередь загрузок:'))
        self.queue_list = QListWidget()
        right_layout.addWidget(self.queue_list)

        qops = QHBoxLayout()
        self.remove_btn = QPushButton('Удалить выбранное')
        self.remove_btn.clicked.connect(self.remove_selected)
        qops.addWidget(self.remove_btn)

        self.clear_console_btn = QPushButton('Очистить консоль')
        self.clear_console_btn.clicked.connect(lambda: self.console.clear())
        qops.addWidget(self.clear_console_btn)
        right_layout.addLayout(qops)

        right_layout.addWidget(QLabel('Консоль:'))
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFixedHeight(220)
        right_layout.addWidget(self.console)

        self.global_progress = QProgressBar()
        right_layout.addWidget(self.global_progress)

        # Layout using splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(main)
        layout.addWidget(splitter)

    # ----------------- UI actions -----------------
    def add_single(self):
        text = self.single_input.text().strip()
        if not text:
            QMessageBox.warning(self, 'Ошибка', 'Ссылка пустая')
            return
        links = self._extract_urls(text)
        self._add_links(links)
        self.single_input.clear()

    def add_batch(self):
        text = self.batch_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, 'Ошибка', 'Нет текста для добавления')
            return
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        links = []
        for l in lines:
            links += self._extract_urls(l)
        if not links:
            QMessageBox.information(self, 'Ничего не найдено', 'Не найдено ссылок rutube в тексте')
            return
        self._add_links(links)
        self.batch_input.clear()

    def extract_links(self):
        text = self.rich_input.toPlainText()
        links = self._extract_urls(text)
        if not links:
            QMessageBox.information(self, 'Ничего не найдено', 'Не найдено ссылок rutube в тексте')
            return
        self._add_links(links)
        self.rich_input.clear()

    def _extract_urls(self, text: str):
        """
        Возвращает все rutube-ссылки из текста, включая длинные с параметрами.
        Убирает дубликаты, но НЕ обрезает параметры.
        """
        urls = RUTUBE_LINK_RE.findall(text)
        return list(dict.fromkeys(urls))  # сохраняем порядок и уникальность


    def _add_links(self, links):
        added = 0
        for link in links:
            if link in self.queued_urls:
                continue
            item = QListWidgetItem(link)
            item.setData(Qt.UserRole, {
                'url': link,
                'status': 'queued',
                'progress': 0,
                'filepath': ''
            })
            self.queue_list.addItem(item)
            self.queued_urls.append(link)
            added += 1
            # Лог для отладки полноты ссылок
            self.signals.append_console.emit(f"[DEBUG] Добавлена полная ссылка: {link}")
        self.signals.append_console.emit(f'Добавлено ссылок: {added}')


    def remove_selected(self):
        for item in list(self.queue_list.selectedItems()):
            data = item.data(Qt.UserRole)
            url = data.get('url')
            self.queued_urls.remove(url)
            self.queue_list.takeItem(self.queue_list.row(item))
            self.signals.append_console.emit(f'Удалено из очереди: {url}')

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, 'Выбрать папку для сохранения', self.download_dir)
        if d:
            self.download_dir = d
            self.dir_btn.setText(self.download_dir)

    def start_downloads(self):
        if not self.queued_urls:
            QMessageBox.warning(self, 'Очередь пуста', 'Добавьте ссылки перед стартом')
            return
        if not YT_DLP_AVAILABLE:
            QMessageBox.warning(self, 'yt-dlp не установлен', 'Пожалуйста, установите yt-dlp через кнопку "Установить yt-dlp"')
            return
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.install_btn.setEnabled(False)
        max_workers = self.threads_spin.value()
        self.global_progress.setValue(0)
        self.global_progress.setMaximum(len(self.queued_urls))
        self.running_count = 0
        self._submit_all(max_workers)

    def _submit_all(self, max_workers: int):
        # Use threadpool to run multiple DownloadWorker
        # We will not create more than max_workers at the same time
        self.signals.append_console.emit(f'Запуск скачивания {len(self.queued_urls)} файлов ({max_workers} потоков)')
        # Launch up to max_workers tasks simultaneously using the threadpool directly
        # We'll just submit all tasks; QThreadPool will limit concurrency by maxThreadCount
        self.threadpool.setMaxThreadCount(max_workers)
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            data = item.data(Qt.UserRole)
            url = data['url']
            ydl_opts = {
                'format': 'best',
                # 'noplaylist': True,
                'outtmpl': os.path.join(self.download_dir, '%(title)s.%(ext)s'),
            }
            worker = DownloadWorker(url, self.download_dir, ydl_opts, self.signals)
            self.threadpool.start(worker)
            self.running_count += 1
            self._set_item_status(url, 'running')

    def stop_downloads(self):
        # QThreadPool doesn't provide kill; this is a polite stop: set flag and rely on yt-dlp to be interruptible
        # For now, ask user to confirm and then reset threadpool by creating a new one
        reply = QMessageBox.question(self, 'Остановить', 'Остановить все текущие загрузки? (будут прерваны)')
        if reply == QMessageBox.Yes:
            # Recreate threadpool to stop queued tasks (running tasks won't be killed immediately)
            self.threadpool = QThreadPool()
            self.threadpool.setMaxThreadCount(self.threads_spin.value())
            self.signals.append_console.emit('Загрузка остановлена пользователем (остановлены новые задания).')
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.install_btn.setEnabled(True)

    def reset_all(self):
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Question)
        dlg.setWindowTitle('Сброс')
        dlg.setText('Очистить очередь и консоль?\n\nУдалить загруженные файлы этой сессии?')
        delete_cb = QCheckBox('Удалить загруженные файлы')
        dlg.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        dlg.layout().addWidget(delete_cb)
        res = dlg.exec_()
        if res == QMessageBox.Cancel:
            return
        if res == QMessageBox.Yes:
            if delete_cb.isChecked():
                # Delete recorded downloaded files
                removed = 0
                for f in list(self.downloaded_files):
                    try:
                        if os.path.isfile(f):
                            os.remove(f)
                            removed += 1
                    except Exception as e:
                        self.signals.append_console.emit(f'Ошибка при удалении {f}: {e}')
                self.signals.append_console.emit(f'Удалено файлов: {removed}')
            # fall through to clear
        # Clear queue and console
        self.queue_list.clear()
        self.queued_urls.clear()
        self.console.clear()
        self.downloaded_files.clear()
        self.global_progress.setValue(0)

    def check_compatibility(self):
        import platform
        py_ver = platform.python_version()
        if YT_DLP_AVAILABLE:
            try:
                import yt_dlp
                yt_ver = yt_dlp.version.__version__
            except Exception:
                yt_ver = "Unknown"
            self.signals.append_console.emit(f"Python: {py_ver}, yt-dlp: {yt_ver}")
            if sys.version_info < (3, 10):
                self.signals.append_console.emit(
                    "[WARNING] Ваша версия Python ниже 3.10. "
                    "Некоторые новые сборки yt-dlp несовместимы с этим Python.\n"
                    "Рекомендую:\n"
                    "pip install yt-dlp==2023.12.30"
                )
        else:
            self.signals.append_console.emit("yt-dlp не импортируется. Возможные причины:\n"
                                         "- не установлен\n"
                                         "- несовместимая версия с этим Python\n"
                                         "Попробуйте: pip install yt-dlp==2023.12.30")

    def install_yt_dlp(self):
        # Use QProcess to run pip install with the same python interpreter
        python = sys.executable
        args = [python, '-m', 'pip', 'install', 'yt-dlp']
        self.signals.append_console.emit('Запуск установки: ' + ' '.join(args))
        self.install_btn.setEnabled(False)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_pip_output)
        self.proc.finished.connect(self._on_pip_finished)
        self.proc.start(python, args[1:])

    def _on_pip_output(self):
        data = self.proc.readAllStandardOutput().data().decode('utf-8', errors='ignore')
        self.signals.append_console.emit(data)

    def _on_pip_finished(self):
        code = self.proc.exitCode()
        self.signals.append_console.emit(f'pip finished with code {code}')
        self.install_btn.setEnabled(True)
        # Try reloading
        global YT_DLP_AVAILABLE, YoutubeDL
        try:
            import importlib
            mod = importlib.import_module('yt_dlp')
            YoutubeDL = getattr(mod, 'YoutubeDL')
            YT_DLP_AVAILABLE = True
            self.signals.append_console.emit('yt-dlp успешно импортирован')
        except Exception as e:
            self.signals.append_console.emit(f'Не удалось импортировать yt-dlp: {e}')
            YT_DLP_AVAILABLE = False
        self._refresh_yt_dlp_state()

    # ----------------- signal slots -----------------
    def _append_console(self, text: str):
        self.console.append(text)

    def _on_item_progress(self, url: str, percent: int):
        # find list item and update its text/progress bar if any
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            data = item.data(Qt.UserRole)
            if data.get('url') == url:
                data['progress'] = percent
                item.setData(Qt.UserRole, data)
                item.setText(f"{url}  — {percent}%")
                break

    def _set_item_status(self, url: str, status: str):
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            data = item.data(Qt.UserRole)
            if data.get('url') == url:
                data['status'] = status
                item.setData(Qt.UserRole, data)
                item.setText(f"{url}  — {status}")
                break

    def _on_item_status(self, url: str, status: str):
        self._set_item_status(url, status)

    def _on_item_finished(self, url: str, filepath: str):
        # store downloaded file path
        if filepath:
            # If yt-dlp provided temp or absolute path, try to sanitize and record
            if os.path.isfile(filepath):
                self.downloaded_files.append(filepath)
            else:
                # try to find matching file in download dir by basename
                b = os.path.basename(filepath)
                cand = os.path.join(self.download_dir, b)
                if os.path.isfile(cand):
                    self.downloaded_files.append(cand)
        # update progress and global progress
        # mark as done
        self._set_item_status(url, 'completed')
        cur = self.global_progress.value()
        self.global_progress.setValue(min(self.global_progress.maximum(), cur + 1))

    def _set_ui_enabled(self, enabled: bool):
        self.start_btn.setEnabled(enabled)
        self.install_btn.setEnabled(enabled)

    def _refresh_yt_dlp_state(self):
        if YT_DLP_AVAILABLE:
            self.yt_label.setText('yt-dlp: установлен')
            self.install_btn.setEnabled(False)
        else:
            self.yt_label.setText('yt-dlp: НЕ установлен')
            self.install_btn.setEnabled(True)

    def show_instructions(self):
        text = (
            "Инструкция:\n\n"
            "1) Вставьте одну ссылку в поле '1)' и нажмите 'Добавить в очередь'.\n"
            "2) Или вставьте несколько ссылок по одной на строку в поле '2)' и нажмите 'Добавить все'.\n"
            "3) Вставьте произвольный текст (страницу, сообщение) в поле '3)' и нажмите 'Извлечь ссылки',\n"
            "   приложение автоматически найдёт rutube-ссылки и добавит их в очередь.\n\n"
            "Доступные опции:\n"
            " - Потоки: количество параллельных загрузок.\n"
            " - Папка: куда сохранять файлы. По умолчанию папка ./downloads.\n\n"
            "Консоль показывает ход операций и ошибки. При необходимости нажмите 'Установить yt-dlp',\n"
            "чтобы установить зависимость.\n\n"
            "Сброс: очищает очередь и консоль; при выборе опции удалит все файлы, загруженные в этой сессии.\n\n"
            "Примечание: некоторые ссылки rutube ведут на плейлисты — в этом случае будет загружен весь плейлист.\n"
            "Если хотите только отдельное видео, используйте прямую ссылку на видео."
        )
        QMessageBox.information(self, 'Инструкция', text)

    def _apply_theme(self, name: str):
        name = name or self.theme_combo.currentText()
        if name == 'Dark':
            style = """
            QWidget { background: #1e1e1e; color: #e0e0e0; }
            QPushButton { background: #3a3a3a; border-radius: 6px; padding: 8px; }
            QLineEdit, QPlainTextEdit, QTextEdit { background: #2b2b2b; color: #e0e0e0; }
            QListWidget { background: #212121; }
            QProgressBar { background: #2b2b2b; }
            """
        elif name == 'Light':
            style = """
            QWidget { background: #f7f7f7; color: #222; }
            QPushButton { background: #e7e7e7; border-radius: 6px; padding: 8px; }
            QLineEdit, QPlainTextEdit, QTextEdit { background: white; color: #111; }
            QListWidget { background: white; }
            QProgressBar { background: #f0f0f0; }
            """
        else:  # Blue
            style = """
            QWidget { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #e6f0ff, stop:1 #eef6ff); color: #03396c; }
            QPushButton { background: #8fbce6; border-radius: 6px; padding: 8px; }
            QLineEdit, QPlainTextEdit, QTextEdit { background: white; color: #03396c; }
            QListWidget { background: white; }
            QProgressBar { background: #dfeffd; }
            """
        self.setStyleSheet(style)


def main():
    app = QApplication(sys.argv)
    # Use Fusion style for consistent look across platforms
    QApplication.setStyle(QStyleFactory.create('Fusion'))
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
