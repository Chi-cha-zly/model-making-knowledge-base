"""Local OCR adapter. No cloud model or network calls."""
from pathlib import Path
import re
import subprocess


class TesseractOCR:
    def __init__(self, *, executable='tesseract', languages='chi_sim+eng', tessdata_dir=None, timeout=90):
        self.executable = executable
        self.languages = languages
        self.tessdata_dir = Path(tessdata_dir).resolve() if tessdata_dir else None
        self.timeout = timeout

    def __call__(self, path):
        command = [self.executable, str(Path(path).resolve()), 'stdout', '-l', self.languages,
                   '--oem', '1', '--psm', '11']
        if self.tessdata_dir:
            command += ['--tessdata-dir', str(self.tessdata_dir)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    check=False, timeout=self.timeout)
        except FileNotFoundError:
            raise RuntimeError('Tesseract executable is unavailable') from None
        except subprocess.TimeoutExpired:
            raise RuntimeError('Tesseract OCR timed out') from None
        if result.returncode:
            raise RuntimeError('Tesseract OCR failed; verify image and installed language data')
        # Tesseract may put spaces between every Chinese character. Join only adjacent
        # CJK characters; preserve Latin words, paint identifiers and line boundaries.
        return re.sub(r'(?<=[\u3400-\u9fff])[^\S\n]+(?=[\u3400-\u9fff])', '', result.stdout).strip()
