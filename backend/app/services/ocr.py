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


# No fallback: callers can explicitly choose Tesseract on other platforms.
# Confidence remains diagnostic metadata; Chinese text is never dropped by a cutoff.
def render_observations(lines):
    return '\n'.join(line['text'].strip() for line in lines if line['text'].strip())


def column_regions(lines):
    """Conservative repeated-left-edge columns within the same vertical text band.

    A lone long line or two paragraphs never establishes a multi-column layout.
    Re-recognition is needed because a detector may join text across a gutter.
    """
    from statistics import median
    groups = []
    for line in sorted(lines, key=lambda v:v['x']):
        if len(line['text'].strip()) < 4 or not .008 <= line['h'] <= .07:
            continue
        group = next((g for g in groups if abs(median(v['x'] for v in g)-line['x']) < .018), None)
        if group is None:
            groups.append([line])
        else:
            group.append(line)
    runs = []
    for group in groups:
        run = []
        for line in sorted(group,key=lambda v:-v['y']):
            if run and (run[-1]['y']-line['y'] > max(line['h'],run[-1]['h'])*2.5
                        or max(line['h'],run[-1]['h']) > min(line['h'],run[-1]['h'])*1.5):
                if len(run)>=3: runs.append(run)
                run=[]
            run.append(line)
        if len(run)>=3: runs.append(run)
    if len(runs)<3:
        return []
    def band(run):
        return min(v['y'] for v in run), max(v['y']+v['h'] for v in run)
    candidates=[]
    for anchor in runs:
        lo,hi=band(anchor)
        aligned=[run for run in runs if min(hi,band(run)[1])-max(lo,band(run)[0]) >= min(hi-lo,band(run)[1]-band(run)[0])*.6]
        if len(aligned)>=3:
            candidates.append(aligned)
    if not candidates:
        return []
    columns=sorted(max(candidates,key=lambda v:sum(len(g) for g in v)), key=lambda g:median(v['x'] for v in g))
    starts=[median(v['x'] for v in g) for g in columns]
    if any(b-a<.055 for a,b in zip(starts,starts[1:])):
        return []
    lo=max(0,min(band(g)[0] for g in columns)-.012)
    hi=min(1,max(band(g)[1] for g in columns)+.012)
    regions=[]
    for i,(start,group) in enumerate(zip(starts,columns)):
        left=max(0,start-.006)
        right=starts[i+1]-.006 if i+1<len(starts) else min(1,max(v['x']+v['w'] for v in group)+.008)
        # Reject layouts where most genuine lines span the proposed gutter.
        if sum(v['x']+v['w']>right+.015 for v in group)>len(group)//2:
            return []
        regions.append([left,lo,right-left,hi-lo])
    return regions


class VisionOCR:
    """macOS accurate OCR, with geometry-based re-reading of multi-column text."""
    def __init__(self, *, timeout=120, language_correction=True):
        self.timeout=timeout
        self.language_correction=language_correction

    def _executable(self):
        import hashlib
        import os
        import platform
        import tempfile
        if platform.system() != 'Darwin':
            raise RuntimeError('Apple Vision OCR requires macOS; choose tesseract explicitly on this platform')
        source=Path(__file__).with_name('vision_ocr.swift')
        key=hashlib.sha256(source.read_bytes()+platform.machine().encode()+platform.mac_ver()[0].encode()).hexdigest()[:24]
        # Persistent cache in the OS user temp directory. Reject shared/symlink
        # directories before executing cached code; source hash changes invalidate it.
        import stat
        cache=Path(tempfile.gettempdir())/f'modelkb-vision-{os.getuid()}'
        cache.mkdir(mode=0o700,exist_ok=True)
        info=cache.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise RuntimeError('Apple Vision OCR cache is not private')
        target=cache/f'ocr-{key}'
        if target.exists():
            info=target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
                raise RuntimeError('Apple Vision OCR binary cache is unsafe')
            return target
        fd,name=tempfile.mkstemp(dir=cache,prefix='build-')
        os.close(fd)
        temporary=Path(name)
        try:
            result=subprocess.run(['/usr/bin/swiftc','-module-cache-path',str(cache/'modules'),str(source),'-o',str(temporary)],capture_output=True,text=True,timeout=180)
            if result.returncode:
                raise RuntimeError('Apple Vision OCR compilation failed; install compatible Xcode command line tools')
            temporary.chmod(0o700)
            os.replace(temporary,target)
        except FileNotFoundError:
            raise RuntimeError('Apple Vision OCR requires Swift command line tools') from None
        except subprocess.TimeoutExpired:
            raise RuntimeError('Apple Vision OCR compiler timed out') from None
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _recognize(self,path,region=None,languages=None):
        import json
        import math
        payload={'path':str(Path(path).resolve()),'language_correction':self.language_correction}
        if region: payload['region']=region
        if languages: payload['languages']=languages
        try:
            result=subprocess.run([str(self._executable())],input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Apple Vision OCR timed out') from None
        if result.returncode:
            raise RuntimeError('Apple Vision OCR failed; verify local Vision service access and image format')
        try:
            value=json.loads(result.stdout)
            if value['engine']!='apple-vision' or not isinstance(value['revision'],int) or not isinstance(value['observations'],list):
                raise ValueError()
            for line in value['observations']:
                if not isinstance(line['text'],str) or any(not isinstance(line[k],(int,float)) or not math.isfinite(line[k]) or not 0<=line[k]<=1.001 for k in ('x','y','w','h','confidence')):
                    raise ValueError()
            if sum(len(l['text']) for l in value['observations'])>100000:
                raise ValueError()
            return value
        except (ValueError,KeyError,TypeError):
            raise RuntimeError('Apple Vision OCR returned an invalid response') from None

    def recognize(self,path):
        result=self._recognize(path)
        lines=result['observations']
        original=render_observations(lines)
        # Chinese-first is required for Chinese, but harms clear English-only labels.
        # Re-run a predominantly Latin image in English; never splice/guess letters.
        latin_count=len(re.findall(r'[A-Za-z]',original))
        paint_codes=bool(re.search(r'\b[A-Za-z]{1,3}\d{4,6}\b',original))
        if (latin_count >= 30 or latin_count >= 12 and paint_codes) and not re.search(r'[\u3400-\u9fff]',original):
            english=self._recognize(path,languages=['en-US'])
            if english['observations']:
                result=english;lines=result['observations']
                result['language_pass']='en-US'
        regions=column_regions(lines)
        result['regions']=regions
        if regions:
            # Keep every outside observation. Each text column becomes a paragraph.
            left,lo=regions[0][:2]
            right=regions[-1][0]+regions[-1][2]
            hi=lo+regions[0][3]
            inside=lambda l: left<=l['x']+l['w']/2<=right and lo<=l['y']+l['h']/2<=hi
            replacement=[]
            for region in regions:
                values=self._recognize(path,region)['observations']
                if not values:
                    raise RuntimeError('Apple Vision OCR could not re-read a detected text column')
                replacement.append(values)
            before=[]; after=[]; found=False
            for line in lines:
                if inside(line): found=True
                elif found: after.append(line)
                else: before.append(line)
            result['text']='\n\n'.join(filter(None,[render_observations(before),*(render_observations(v) for v in replacement),render_observations(after)]))
            result['observations']=before+[l for group in replacement for l in group]+after
        else:
            result['text']=render_observations(lines)
        if len(result['text'])>99000:
            raise RuntimeError('Apple Vision OCR output exceeds the article limit')
        result['raw_text']=result['text']
        result['status']=ocr_quality(result['observations'])
        if result['status']=='needs_review':
            result['text']=''
        return result

    def __call__(self,path):
        return self.recognize(path)['text']


def local_ocr(*, engine='vision', tessdata_dir=None, languages='chi_sim+eng'):
    if engine=='vision':
        return VisionOCR()
    if engine=='tesseract':
        return TesseractOCR(tessdata_dir=tessdata_dir,languages=languages)
    raise ValueError('Unknown local OCR engine')


def ocr_quality(lines):
    if not lines:
        return 'empty'
    # These narrow failure modes are supported by image inspection, not a general
    # confidence cutoff. A low-confidence Chinese paragraph/paint code is retained.
    vertical=all(l['confidence']<=.5 and l['h']>2*l['w'] for l in lines)
    isolated=len(lines)<=2 and sum(len(l['text'].strip()) for l in lines)<=2 and all(l['confidence']<.5 for l in lines)
    return 'needs_review' if vertical or isolated else 'completed'


def recognize_local(recognizer,path):
    if hasattr(recognizer,'recognize'):
        result=recognizer.recognize(path)
    else:
        text=recognizer(path)
        result={'text':text,'status':'completed' if text else 'empty','engine':type(recognizer).__name__}
    if not isinstance(result.get('text'),str) or len(result['text'])>99000 or result.get('status') not in ('completed','empty','needs_review'):
        raise ValueError('Invalid local OCR result')
    return result
