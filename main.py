import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
)

from PySide6.QtWidgets import QApplication
from lyset.gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Lyset')
    app.setOrganizationName('sigurdvanhauen')
    # Fusion style is cross-platform and ignores the OS dark-mode palette,
    # so our explicit stylesheet colours are always applied correctly.
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
