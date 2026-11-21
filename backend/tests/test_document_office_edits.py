"""真实 DOCX 原包的图片、表格、样式和审阅选择回归。"""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from docx.shared import Mm
from PIL import Image

from app.services.documents.office_edit import (
    OfficeEditError,
    OfficeEditPlan,
    apply_office_edits,
    inspect_office_document,
    office_asset_thumbnail,
    office_image_bytes,
    office_vision_images,
)


@pytest.fixture
def original_word(tmp_path):
    images = {}
    for color in ("red", "orange", "blue"):
        path = tmp_path / f"{color}.png"
        Image.new("RGB", (110, 55), color).save(path)
        images[color] = path
    original = tmp_path / "原始报告.docx"
    doc = Document()
    doc.sections[0].left_margin = Mm(28)
    doc.add_paragraph("项目报告", style="Title")
    doc.add_heading("模型方法", level=1)
    method = doc.add_paragraph()
    method.add_run("  机器")
    method.add_run("学习").bold = True
    method.add_run("用于故障分类  ")
    doc.add_picture(str(images["red"]), width=Mm(58))._inline.docPr.set("descr", "主图原说明")
    doc.add_paragraph("图 1　设备示意", style="Caption")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "设备编号"
    cell = table.cell(0, 1).paragraphs[0]
    cell.add_run("设备接线：")
    cell.add_run().add_picture(str(images["red"]), width=Mm(28))
    doc.add_picture(str(images["orange"]), width=Mm(30))
    doc.sections[0].header.paragraphs[0].add_run().add_picture(str(images["red"]), width=Mm(20))
    doc.sections[0].footer.paragraphs[0].text = "技术团队"
    doc.save(original)
    return original, images


def _image_colors(path: Path):
    colors = []
    for info in inspect_office_document(path).images:
        data, media = office_image_bytes(path, info.id)
        assert media in {"image/png", "image/jpeg"}
        with Image.open(BytesIO(data)) as image:
            colors.append(image.convert("RGB").getpixel((0, 0)))
    return colors


def _plan(operations: list[dict]):
    return OfficeEditPlan.model_validate({"summary": "核对模型段落和原图", "operations": operations})


def test_original_word_edit_preserves_layout_and_shared_media(original_word, tmp_path):
    source, photos = original_word
    original = inspect_office_document(source)
    assert original.title == "项目报告"
    assert original.images[0].caption == "图 1　设备示意"
    assert original.images[0].width_pixels == 110 and original.images[0].height_pixels == 55
    assert original.images[0].checksum == original.images[1].checksum == original.images[3].checksum
    text = next(p for p in original.paragraphs if p.text.strip().startswith("机器学习"))
    assert text.section == "模型方法"
    assert text.text.startswith("  ")
    edited = tmp_path / "改动后.docx"
    initial_digest = sha256(source.read_bytes()).digest()
    result = apply_office_edits(source, _plan([
        {"operation": "replace_text", "target_id": text.id, "before": "机器学习", "after": "深度学习"},
        {"operation": "replace_image", "target_id": "img1", "asset_id": "attachment_2"},
        {"operation": "resize_image", "target_id": "img1", "width_mm": 70},
        {"operation": "set_image_alt", "target_id": "img1", "alt": "设备换装蓝色示意图"},
        {"operation": "remove_image", "target_id": "img3"},
    ]), edited, image_sources={"attachment_2": photos["blue"]})
    assert result.validation.passed
    assert sha256(source.read_bytes()).digest() == initial_digest
    assert _image_colors(edited) == [(0, 0, 255), (255, 0, 0), (255, 0, 0)]
    changes = {change.id: change for change in result.changes}
    replacement = changes["change_2"]
    assert replacement.replacement_asset_id == "attachment_2"
    assert replacement.before.id == replacement.after.id == "img1"
    assert replacement.before.checksum != replacement.after.checksum
    assert changes["change_3"].after.width_mm == pytest.approx(70)
    assert changes["change_3"].after.height_mm == pytest.approx(35)
    assert changes["change_4"].after.alt == "设备换装蓝色示意图"
    assert changes["change_5"].before.id == "img3" and changes["change_5"].after is None

    doc = Document(edited)
    assert doc.sections[0].left_margin == Document(source).sections[0].left_margin
    assert doc.sections[0].footer.paragraphs[0].text == "技术团队"
    assert doc.tables[0].cell(0, 0).text == "设备编号"
    paragraph = next(p for p in doc.paragraphs if p.text.strip().startswith("深度学习"))
    assert paragraph.text == "  深度学习用于故障分类  "
    assert paragraph.runs[1].bold and paragraph.runs[1].text == "学习"
    with ZipFile(source) as before, ZipFile(edited) as after:
        for path in ("word/styles.xml", "word/settings.xml", "word/header1.xml", "word/footer1.xml"):
            assert before.read(path) == after.read(path)
        all_media = {hashsum for path in after.namelist() if path.startswith("word/media/")
                     for hashsum in [sha256(after.read(path)).hexdigest()]}
        assert original.images[2].checksum not in all_media
        assert original.images[0].checksum in all_media


def test_partial_review_applies_only_approved_changes(original_word, tmp_path):
    source, _ = original_word
    edited = tmp_path / "仅批准第二项.docx"
    candidate = _plan([
        {"operation": "remove_image", "target_id": "img1"},
        {"operation": "set_image_alt", "target_id": "img2", "alt": "图 2 附加说明"},
    ])
    result = apply_office_edits(source, candidate, edited, accepted_ids={"change_2"})
    assert [change.id for change in result.changes] == ["change_2"]
    assert result.document.images[1].alt == "图 2 附加说明"
    assert len(result.document.images) == 4
    all_edits = apply_office_edits(source, candidate, tmp_path / "候选.docx")
    assert all_edits.changes[1].before.id == "img2"
    assert all_edits.changes[1].after.id == "img1"
    assert len(all_edits.document.images) == 3


def test_word_vision_uses_selected_embedded_raster(original_word):
    source, _ = original_word
    assert [item[0] for item in office_vision_images(source, {"img2", "img3"}, limit=1)] == ["img2"]
    assert office_vision_images(source, {"missing"}) == []
    with pytest.raises(OfficeEditError, match="图片不存在"):
        office_image_bytes(source, "../secret.png")


def test_insert_image_into_original_paragraph_and_review_anchor(original_word, tmp_path):
    source, photos = original_word
    original = inspect_office_document(source)
    anchor = next(p for p in original.paragraphs if p.text == "技术团队")
    result_path = tmp_path / "插入后.docx"
    result = apply_office_edits(source, _plan([
        {"operation": "insert_image", "target_id": anchor.id, "asset_id": "photo",
         "width_mm": 16, "alt": "新增页脚标记"},
    ]), result_path, image_sources={"photo": photos["blue"]})
    change = result.changes[0]
    assert change.before is None and change.after.id == "img4"
    assert change.after.paragraph_id == anchor.id
    assert change.after.width_mm == 16 and change.after.height_mm == 8
    assert change.after.alt == "新增页脚标记"
    assert change.replacement_asset_id == "photo"
    assert Document(result_path).sections[0].footer.paragraphs[0].text == "技术团队"
    with ZipFile(source) as before, ZipFile(result_path) as after:
        assert before.read("word/document.xml") == after.read("word/document.xml")
        assert "word/_rels/footer1.xml.rels" not in before.namelist()
        assert "word/_rels/footer1.xml.rels" in after.namelist()
    assert _image_colors(result_path)[3] == (0, 0, 255)
    thumbnail, mime = office_asset_thumbnail(photos["blue"])
    assert mime == "image/jpeg"
    with Image.open(BytesIO(thumbnail)) as image:
        assert image.size == (110, 55)


def test_word_edit_rejects_missing_assets_and_overlapping_edits(original_word, tmp_path):
    source, _ = original_word
    path = tmp_path / "output.docx"
    operation = {"operation": "replace_image", "target_id": "img1", "asset_id": "foreign"}
    with pytest.raises(OfficeEditError, match="已经校验的附件"):
        apply_office_edits(source, _plan([operation]), path)
    assert not path.exists()
    paragraph = next(p for p in inspect_office_document(source).paragraphs if "机器学习" in p.text)
    with pytest.raises(OfficeEditError, match="重叠"):
        apply_office_edits(source, _plan([
            {"operation": "replace_text", "target_id": paragraph.id, "before": "机器学习", "after": "深度学习"},
            {"operation": "replace_text", "target_id": paragraph.id, "before": "机器", "after": "设备"},
        ]), path)
    assert not path.exists()
    with pytest.raises(FileExistsError):
        apply_office_edits(source, _plan([{"operation": "remove_image", "target_id": "img1"}]), source)
    assert len(inspect_office_document(source).images) == 4
