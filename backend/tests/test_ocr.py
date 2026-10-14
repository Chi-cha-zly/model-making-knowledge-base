import json
import subprocess
from pathlib import Path
import pytest


def line(text, x, y, w=.12, h=.02):
    return dict(text=text, x=x, y=y, w=w, h=h, confidence=.15)


def test_columns_detected_without_discarding_low_confidence_chinese():
    from app.services.ocr import column_regions, render_observations
    rows = [line('完整中文段落内容', x, y) for x in (.01,.15,.29,.43) for y in (.58,.55,.52,.49)]
    rows.append(line('这里跨栏 合并文字', .29,.46,.26))
    regions = column_regions(rows)
    assert len(regions) == 4
    assert all(r[2] < .15 for r in regions)
    assert '完整中文段落内容' in render_observations(rows)


def test_two_separate_paragraphs_are_not_guessed_into_columns():
    from app.services.ocr import column_regions
    rows = [line('上方跨宽度的中文段落',.35,y,.6,.04) for y in (.88,.83,.78)]
    rows += [line('下方另一段完整中文文字',.07,y,.45,.04) for y in (.4,.35,.3)]
    assert column_regions(rows) == []


def test_vision_failure_never_becomes_empty_ocr(tmp_path, monkeypatch):
    from app.services.ocr import VisionOCR
    ocr = VisionOCR()
    monkeypatch.setattr(ocr, '_executable', lambda: Path('/fake/native'))
    monkeypatch.setattr(subprocess, 'run', lambda *a,**k: subprocess.CompletedProcess(a,0,'{}',''))
    with pytest.raises(RuntimeError, match='response'):
        ocr(tmp_path/'image.png')


def test_vision_valid_empty_and_no_fallback(tmp_path, monkeypatch):
    from app.services.ocr import VisionOCR
    ocr=VisionOCR()
    monkeypatch.setattr(ocr, '_executable', lambda: Path('/fake/native'))
    monkeypatch.setattr(subprocess, 'run', lambda *a,**k: subprocess.CompletedProcess(a,0,'{"observations":[],"engine":"apple-vision","revision":3}',''))
    assert ocr(tmp_path/'image.png') == ''
    monkeypatch.setattr(subprocess, 'run', lambda *a,**k: subprocess.CompletedProcess(a,1,'','native unavailable'))
    with pytest.raises(RuntimeError, match='failed'):
        ocr(tmp_path/'image.png')


def test_quality_gate_keeps_real_low_confidence_text_and_reports_unsupported_geometry():
    from app.services.ocr import ocr_quality
    assert ocr_quality([line('全文低分中文段落不能被删除',.1,.5)])=='completed'
    assert ocr_quality([line('AK11166',.1,.5)])=='completed'
    assert ocr_quality([line('國',.1,.5)])=='needs_review'
    assert ocr_quality([line('纵向无法可靠识别',.1,.1,.04,.5)])=='needs_review'
    assert ocr_quality([])=='empty'


def test_english_pass_never_discards_short_chinese_labels(monkeypatch,tmp_path):
    from app.services.ocr import VisionOCR
    ocr=VisionOCR();calls=[]
    def recognize(path,region=None,languages=None):
        calls.append(languages)
        return {'engine':'apple-vision','revision':3,'observations':[
            line('ANTHRACITE GREY GRAPHITE SILVER GREY EDGE HIGHLIGHT',.1,.8,.8),line('亮部',.1,.5)]}
    monkeypatch.setattr(ocr,'_recognize',recognize)
    assert '亮部' in ocr(tmp_path/'image.png')
    assert calls==[None]


def test_multicolumn_core_does_not_crop_larger_spanning_footer():
    from app.services.ocr import column_regions
    rows=[line('完整中文段落内容',x,y) for x in (.01,.15,.29,.43) for y in (.58,.55,.52,.49)]
    rows += [line('横跨两栏的较大号说明',.01,y,.25,.032) for y in (.39,.34,.29)]
    regions=column_regions(rows)
    assert regions and min(r[1] for r in regions)>.4
