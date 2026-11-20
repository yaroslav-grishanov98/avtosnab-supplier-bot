"""Cross-domain fixtures exercise format readers and loss/error boundaries."""

import base64
import zlib
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from docx import Document
from openpyxl import Workbook
from rest_framework.exceptions import ValidationError

from api.v1.catalog.importers import parse_catalog_file
from api.v1.suppliers.normalization import extract_price_rows_from_file
from api.v1.suppliers.product_import_parsing import SupplierProductImportParseError, parse_supplier_product_import_file
from file_imports import extraction

# A real BIFF8 workbook, generated in memory: header plus Мука / кг / 60.
_BIFF8 = "eJztWE1oU0EQ/vZl0yahTZOaCK1aQsGotYhaxVBMX1W0B7GkXhQRNG1ykJZUYj3oQas1R0HwpHgp9OKl6sUf9KA3D0JFD4IgJHr0ZFHw0OY5M0k01QoN2Kqw32NnZ2d2dvbt7O7bfS9ngvnJe60F/IQeuFB0vKirkilK3kohANI7DrOV3EPJMfiv4PVQIOvceNz4op5jyPEuwMJd/Ywo8J7ScZxG/2gmHVlB7JU+JBX3IU5U4RZJ/GiRXjULHRK6SugdqflEaK9IrgqNU928OoYZu78jVp7FR6120fnB7T4Qm7ci2YYwnvMsvnhNleq6sSd7KjnybyradAOmQHHrS2fS2eRIHiEK4BS+OBHgc2WlPo0Y+crKFUj+daG8fhH5dUsD43BOygTP0Z4662KNB4nsaOrs0NiZORykYEOSB2qd3qp36e1E4zqmd2oShfUOvYXajZFiN3EaKkqaONUhdq3ep7uJpfa7dZf2UTOlFR9YsOIbZSU0EE2hSfigrIcAOZ27/enVocGEfUIk47LLl74F67n7cHCJLcjYLxpLKL9Hh/CbhV6WVtcI3yo0RC9FeTQRLjMHJqTOFdFGyU+X4LW9oYrfSHzu48DDttwHexPx032F86HpN/Yk2unblCJ7fibQqTrVzRuMR3YlV+V9453Qll/2EI8VKPfdKUVINWEePmGDQkslHh31vWSVx4qt1SLWSqxd5fpKrHncLlhcYutZKvVYzbgvQ9eLH/DBwMDAwMDAwMDA4E+CT6d87OTzKZ9K+aLBNwK+GvB/nXlKxb/9k8Jg2XAYo/SM0QVyPzKUZ3GupvmzGm5VaUst0abyv5BxhLxnMYxB6cdwTb4ZdKNT1e+zZMNAza5+h5r9F2vp5zL7/wYUK8XB"


def workbook_bytes(rows, *, cover=False, second=None):
    workbook = Workbook()
    sheet = workbook.active
    if cover:
        sheet.append(["Экспорт за октябрь"])
        sheet = workbook.create_sheet("Products")
    for row in rows:
        sheet.append(row)
    if second:
        sheet = workbook.create_sheet("Second")
        for row in second:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class ParsingParityTests(SimpleTestCase):
    def assert_domains(self, name, content, expected, *, expected_units=None, mime="application/octet-stream"):
        def upload():
            return SimpleUploadedFile(name, content, content_type=mime)

        catalog = parse_catalog_file(upload()).rows
        supplier = parse_supplier_product_import_file(upload())
        legacy = extract_price_rows_from_file(upload())
        self.assertEqual([row["name"] for row in catalog], expected)
        self.assertEqual([row["name"] for row in supplier], expected)
        self.assertEqual(len(legacy), len(expected))
        self.assertEqual([row["row_number"] for row in catalog], [row["row_number"] for row in supplier])
        self.assertEqual([row.row_number for row in legacy], [row["row_number"] for row in supplier])
        if expected_units:
            self.assertEqual([row.get("unit") for row in catalog], expected_units)
            self.assertEqual([row.get("unit_label") for row in supplier], expected_units)
        return catalog, supplier, legacy

    def test_csv_delimiters_encodings_and_preamble(self):
        for delimiter in (";", ",", "\t", "|"):
            for encoding in ("utf-8-sig", "cp1251", "utf-16", "utf-32"):
                with self.subTest(delimiter=delimiter, encoding=encoding):
                    text = (
                        "Экспорт\n"
                        + delimiter.join(["Наименование товара в УС", "Ед. изм. в УС", "Цена"])
                        + "\n"
                        + delimiter.join(["Мука", "кг", "60"])
                        + "\n"
                    )
                    self.assert_domains("price.csv", text.encode(encoding), ["Мука"], expected_units=["кг"])

    def test_excel_extensions_and_content_detection(self):
        data = workbook_bytes([["Название", "Ед. изм.", "Цена"], ["Мука", "кг", 60]])
        for name in ("price.xlsx", "price.xlsm", "price.xls", "export.bin"):
            with self.subTest(name=name):
                self.assert_domains(name, data, ["Мука"], expected_units=["кг"])

    def test_real_binary_xls(self):
        self.assert_domains("price.xls", zlib.decompress(base64.b64decode(_BIFF8)), ["Мука"], expected_units=["кг"])

    def test_cover_and_multiple_sheets_with_independent_headers(self):
        data = workbook_bytes(
            [["Название", "Ед. изм.", "Цена"], ["Мука", "кг", 60]],
            cover=True,
            second=[["Цена", "Ед. изм.", "Название"], [80, "шт", "Сахар"]],
        )
        self.assert_domains("price.xlsx", data, ["Мука", "Сахар"], expected_units=["кг", "шт"])

    def test_header_after_twelve_preamble_rows(self):
        data = workbook_bytes([["Отчет"]] * 12 + [["Наименование", "Unit", "Price"], ["Мука", "кг", 60]])
        self.assert_domains("price.xlsx", data, ["Мука"], expected_units=["кг"])

    def test_headerless_single_column(self):
        self.assert_domains("price.xlsx", workbook_bytes([["Товар 1"], ["Product A"]]), ["Товар 1", "Product A"])

    def test_english_headers(self):
        self.assert_domains(
            "price.tsv", b"Product name\tUnit\tPrice\nFlour\tkg\t60\n", ["Flour"], expected_units=["kg"]
        )

    def test_docx_multiple_tables_preserve_column_order_and_paragraphs(self):
        document = Document()
        for rows in (
            [["Название", "Ед. изм.", "Цена"], ["Мука", "кг", "60"]],
            [["Цена", "Название", "Ед. изм."], ["80", "Сахар", "шт"]],
        ):
            table = document.add_table(rows=2, cols=3)
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    table.cell(i, j).text = value
        document.add_paragraph("Соль")
        buffer = BytesIO()
        document.save(buffer)
        self.assert_domains("price.docx", buffer.getvalue(), ["Мука", "Сахар", "Соль"])

    def test_txt_tables_and_plain_text(self):
        self.assert_domains("price.txt", "Название|Ед. изм.|Цена\nМука|кг|60".encode(), ["Мука"], expected_units=["кг"])
        self.assert_domains("price.txt", "Мука\nСахар".encode(), ["Мука", "Сахар"])

    def test_repeated_headers_are_not_products(self):
        self.assert_domains(
            "price.csv",
            "Название;Ед. изм.;Цена\nМука;кг;60\nНазвание;Ед. изм.;Цена\nСахар;шт;80".encode(),
            ["Мука", "Сахар"],
            expected_units=["кг", "шт"],
        )

    def test_long_values_are_not_truncated(self):
        name = "А" * 700
        self.assert_domains("price.csv", ("Название;Ед. изм.\n" + name + ";кг").encode(), [name], expected_units=["кг"])

    @override_settings(FILE_IMPORT_MAX_EXTRACTED_ROWS=2)
    def test_raw_limit_is_an_error_in_all_adapters(self):
        content = "Название\nМука\nСахар\nСоль".encode()
        for function, error in (
            (parse_catalog_file, ValidationError),
            (parse_supplier_product_import_file, SupplierProductImportParseError),
            (extract_price_rows_from_file, extraction.FileExtractionError),
        ):
            with self.subTest(function=function.__name__), self.assertRaises(error):
                function(SimpleUploadedFile("price.csv", content))

    def test_uncached_formula_is_actionable_for_all_adapters(self):
        content = workbook_bytes([["Название", "Цена"], ["Мука", "=30*2"]])
        for function, error in (
            (parse_catalog_file, ValidationError),
            (parse_supplier_product_import_file, SupplierProductImportParseError),
            (extract_price_rows_from_file, extraction.FileExtractionError),
        ):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(error, "формулы"):
                function(SimpleUploadedFile("price.xlsx", content))

    def test_binary_doc_converter_is_shared(self):
        with patch.object(extraction.subprocess, "run") as converter:
            converter.return_value.returncode = 0
            converter.return_value.stdout = "Название;Ед. изм.;Цена\nМука;кг;60".encode()
            self.assert_domains("price.doc", b"binary doc fixture", ["Мука"], expected_units=["кг"])
            self.assertEqual(converter.call_count, 3)

    def test_image_ocr_table_uses_common_headers(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        with patch.object(extraction.pytesseract, "image_to_string", return_value="Название;Ед. изм.;Цена\nМука;кг;60"):
            self.assert_domains("price.png", buffer.getvalue(), ["Мука"], expected_units=["кг"])

    @override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="pdfplumber")
    def test_real_pdf_preserves_tables_and_text_on_other_pages(self):
        # Real PDF generated in memory; regression for table/text page loss.
        content = zlib.decompress(
            base64.b64decode(
                "eJzVVdt2okgUfecrqhONxiRQgCgYW4MaElqTaNRc2ktSQgkYpQiXnmQe5gdm3uZ9vnUKNB26zVq91rwNDxS1z6ld5+xTh8p2W9oRzxaZ7N9//fkPuMYe8cMOmoEz7GIfhdgE1AOYxIhW2A1BnnjYDUjkG3if4QEEZLZgqlWG03gg0Ok1U6sx2DVjWEiZGyjAGqEE3DlefsOhYyDAnboGMR3XAtyt46pu4LwDl2iFQUzK9aNZ+OrRyYC++fVALZQrtZWY2qpJTTTUAMhxPIC7wKaDGuQFjOgcAkmRWKEslYBc5FlZVmQwAVwX+XF25fWKa7zOMABJZnHY/NrS9YnRxyGl4mJZuAF+oSnpK2ThxmZsbkYdTGiAlIyEVEa6nBv4yE0oY5wBb6l0qXMqleJHqSj/y1SkVCqx6YKY1GkY4EvibryDt0DXBE0UoiWxUhylFIcahTbxQR65xH1dkSjYpyt8jEKHuK04snyrIkChxEMo8TLki6UDKOYgzL35/by4jV9/I74ZgDyd0Oh+RUI1MyMDU5b3RonF6zgzH/mv4AiMUw0y3t9PtKEneIEN2juRG3jYcOYONinZwAmXOAbD+CNBfOR5tOM4DS2DtJDlH84EXUF7jWs7NPAREBP9isl7ki5EkCKQ053qLEOaAq272m/quiy1sJEURlvS9DcTytTBrhXaQJBgTBSEVMAVc4Z8UkXDW/cm17EN7+I6svMWe7hQp2bnaZCZ7PS+7hYWE/1qxNkrhSfT/OJLs+aMhyeZofWgalnuuYGrB0HbftShJHh7FSjvqJhVT9mSePdVVWfE0py9y5nVPrbusmyU7R3giXJ8lmOfVSc4uBmOpb2H6ydbqHiunWnfjNiofn9irmztKpOTg+7w8tE+P9t9FFaPjUm+wR4+h+P5ZLi6cs+/sJ/1kbjs7ei5m0yx4B8W8n2+/NzowWldcEi1UeudzysnR72T/LSj3o9qsCDV3eliR426f9SokhsNNpoq/11TRfpBUq9nw9PP2iEcjs/Fwdi7vB9Me+2nemjU9O7h8e3ukJ8KomN3Ty8e6s3bYlvvO5XK7M6aVqZnsGksxOaC1VqFXTR2WstP42y28Ol2NM7sVXY+iPvFx3MGAh4y8PsDSpIkSmAOvmMl+ptILO47pghbGK8oW1gRbvuVoLSNlcUtTBGL29j2HrwoprDQR84S+0kl9BZgRlUJYrlkinyZ5w2FL4liWSzKWJ5RKoQlJNd+7TFhsqnr0Pr4Ojw6AqZDW+2ni5GG4c4JKCUXIv1tkhBI6+++8zuOpU/Kj/wwKQYvCSKTzZ5eacy/+GgfFg=="
            )
        )
        self.assert_domains("price.pdf", content, ["Flour", "Salt"])

    def test_name_only_rows_remain_for_user_review(self):
        self.assert_domains(
            "price.csv", "Название;Ед. изм.;Цена\nМука;кг;60\nСоль;;\nСахар;шт;80".encode(), ["Мука", "Соль", "Сахар"]
        )

    def test_formatted_1c_groups_are_not_products_but_unpriced_products_remain(self):
        from openpyxl.styles import Alignment, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        rows = [
            ["Номенклатура, Упаковка", None, "Розничные"],
            [None, None, "руб."],
            [None, None, "Включает НДС"],
            [None, None, "Цена"],
            ["ПРОДОВОЛЬСТВИЕ"],
            ["03 БАКАЛЕЯ"],
            ["АГРОПРОДУКТ"],
            ["Горчица, шт", None, 224],
            ["Соль, кг"],
            ["АГРОПРОДУКТ ОСНОВНОЙ АССОРТИМЕНТ"],
            ["Другая группа"],
            ["Сахар, кг", None, 80],
        ]
        for row in rows:
            sheet.append(row)
        for number, indent in [(5, 2), (6, 4), (7, 6), (10, 6), (11, 6)]:
            cell = sheet.cell(number, 1)
            cell.alignment = Alignment(indent=indent)
            cell.fill = PatternFill("solid", fgColor="FFFFCC")
            sheet.merge_cells(start_row=number, end_row=number, start_column=1, end_column=2)
        for number in (8, 9, 12):
            sheet.cell(number, 1).alignment = Alignment(indent=8)
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        catalog, supplier, legacy = self.assert_domains(
            "price.xlsx", buffer.getvalue(), ["Горчица, шт", "Соль, кг", "Сахар, кг"]
        )
        self.assertEqual([row["row_number"] for row in supplier], [8, 9, 12])
        self.assertEqual([row["price"] for row in supplier], ["224", None, "80"])

    def test_changed_repeated_header_is_not_a_product(self):
        self.assert_domains(
            "price.csv",
            "Наименование;Ед.;Цена\nМука;кг;60\nТовар;Ед.;Цена с НДС\nСоль;кг;80".encode(),
            ["Мука", "Соль"],
        )

    def test_legacy_payload_roundtrip_keeps_headerless_products_and_long_cells(self):
        from api.v1.suppliers.normalization import extracted_rows_from_payload, extracted_rows_to_payload

        name = "Товар " + "А" * 700
        rows = extract_price_rows_from_file(SimpleUploadedFile("price.csv", name.encode()))
        restored = extracted_rows_from_payload(extracted_rows_to_payload(rows))
        self.assertEqual(restored[0].cells["column_1"], name)
        self.assertTrue(restored[0].source["headerless"])

    @override_settings(SUPPLIER_PRICE_PDF_EXTRACTOR="pdfplumber")
    def test_real_scanned_pdf_renders_and_uses_shared_ocr(self):
        content = zlib.decompress(
            base64.b64decode(
                "eJyNV9m2okgWfb9fgTOOTDKIs+I8XBRnBQ0GEZVBhqrMfOgf6H7r9/7WxnszK+3KWqsa1zKIEyfO2XGIYG8SPNfNYwXiLfHvf/3zP9Bcc2zXHwMZ6mmW5gJfU6HQA1JtJTA1y4dg29Eszw5cRUu/YRAK2fL1rVJ5Q7oYhIfd+Vut9qZZ6tOMvwy3gKd17TAA0tfuv2m+oQAI6ViKrRqWDiFrw2panvHTMAWmBj2DIkIg+1+dsLMI/7HPJhwJY72kIl5TGb7Ha27bNh3bemJmIKRt321XcIASTuW03wxFm/daYRTj7msutIeQptAeDBiS00IAz/D3cOnfO+ITs6FfwkAohIw1S/cvEEYzzAu2gQl0DXqDfsDbvMtXTfGfC1NDbwJFn2A939WA+daTo+iydKGiWdh1VsfbluA5Pi48XC6dszTjvBfSSIqjQUVvEDArSeN0vjDcbpKzVo2K7MVR+9iqbAta1QWt9bvgFHMXdTE6xOppjz5dbaZ4MuH59XSwu9VsI1iflpNsDuuddoFNtTjuxppI49u3b5FIlPYPjM6rk4nKjif70S2iV/0Wrl+2dYoB+D1ncStmrO3L2UbXj90FQjx3Igu0kU3UlRkcjY+PFXY7Zadp0RxNOnZ7AEbnwgquayllsFpn5mvrUje3G1HE9m64nstpvwLKMlWgc7G+lxpNYao+4SplJUFsMhmwQ2Z2a7E2uiSSUtT4+TK3L+qa0oupxqlKmLlkwK+G69zUPQk8GXUS1Zg5AsJh3DaKuREQV2mCnfhwxToVtg8ho/m12xQWbaI/PC9n9B6ry4rLpPqXJVuSO+C43U4KEtgn4QKL2UfHuzVNIjvVhu/XHrvgrmjiSM/XTA6PdRZlzeLhkTsCqdkUredq+6GQmou3bWUL6vtMnKfivN9F7OD0PmkmwdA3B8mgVBvMH++9bVA+d6nGidIxtk5P67RFUKXF8dYpJ7fv8YHVPQvTtjKQ6Ea/dLCjZPIQN47IIEzArNPF4ZkVUod5Nhm0uPYqtXF3/tzAo2vbWU4ijfnjIF5VJT1uwM5GBvNoI0pIsV3FxetlbYVXXNbkM0363Dklxp6/RG8MWZWu92EFjhjVrNyvVyw33yQZazHKd8FALjhDb0hklMm+FuHbcs+RGkfUnpt0n5S2u+OCc3e7c16yZiKmFJLVcwLNl7axa53E5yIqxmp3ISZRj1Rum8xeqxN8HhtjRalTvdXsbFFirraQjVRZe1tDfGvVQjdK0+IVvFKdMHCVxMBaEIr+CpyoRLPlPVC70JvNuOXMrOOFMS5gw4w9yq8mvAxbZW27LAieyUjFKDied61qWueSsVzsXrvJ/Q6/no2X28IisrTmzG2+VvhU51hYL5sRvlOqx2PCVS0sMuqqLO74ElLQ07I+JPBZbmQDRbcLj42H3HagWL6aMj/J5QQhVs3TBM8WHuMZ0+0cH0ZGQijktF0P+od2ZDio97BdXtulN732cO8WGsb7enChLomJrspU2lKsVh+rsIOdMCqU8sJOpgyHq3cHJ76t58xu8wgDUmp4ToId2PYgX8aqqaxQxZRBs7ZAx5nYgUq/G71EscK9h6jzyt7IBdu6xQ/YeGVAiPOFh2V6kbVbutf9q4wkTqwQdCe53hYenqSaMsvv7hw+U2wsO7vTuBTvzI88MZxW5+mTUE7NwT57oR81VGrr7Sr8MLNirKIFJyTrpNh18C1SyUX561KXsV000Q4T0xL5cOPcszojeRtcs+ght40OrMwZqTToTLU/3FARMq1eHcmdIJGA6A5VsJK3qn1msYswjZqpVnwyNWrNnHIWiuB9VNkmqeIgUsnmq8TiEUjofr/Z6euWkLb1lArAdKKYZn5odGDKRyYOLDbKnQeCFC7lvEk9KvGunaueBsgND9q4k/Gra+OeH83ihwS/jGZnFSTDen59Hg1OMG/mE2gU2fDuKrsFJ3Gd6l72Vbe/aYrykejX7RmHIin1zDDdPLHK2oVi+d5a0+3WVZYvmzqcz4hBgbJjt/StwbHJudM9XaRdr9TsZFpCN453UmxN8dcuv2k3D9qDaifnJeyiycTwpA3amUxEKjlG3p+U/FQJW3fu21XxgsIbfROei1ZWEmPlNHEZY6DZqiSKXF7U14K4aaDh66nKmthE6oGb3LkVOthF7jS4srZXdceTz6tszIvvl43DieyOqZ1ea3AdSW/S/Ijr1avjNl9/pC4+HE2qSmK39+4I3sf3gsQiRV6vdVeHhOK1rpvqOajs30WRrG6UFuOY/enVcJPZGaBEDBHXRieN994HUYc7MyJc5r2pCWI7usVLJSy5KuaD9sVa4Bcx7cbJb69XJOSZXLPXn4lNrnX6Ry3k6+8s+J25iy/M3Q5JPSRsL2TsUElAyERTDdCyv4QEjYY/skQWcJqkIKaIFRimxDypmQfuk+Ppzxlz7VObeNCHJnkKDuxzhHdtRdD8J9c/BQ2y0L7433m79b1tf28Hz7g/GPwzjmtubLmAMygOSgQDMAYlcY1QGUZjmDOFkYSKyaoKET8UUK0WYrH9UESEFmThAusD0dP+UyTwYa4XDUO+VOI5NPlQIktPm4Yy5tPb+7HOzwBt4IO7rb/EoF5iNAP/YrsQDCzb+mragZcOZ4Sl9w3b4p7IYI7FUZzCUJQMF1QiiCxKpFA09cPvz5NH2tffbVf1IDjshOj+LkhYcjVQQn0F/5SVz9qPDdkF7lcoDx1e5OQhnf6oTaipPgoPB5bnaIpxNjQ1DLYw/Lv2NPrPmw+LCxwn1KdIF9y910LS/7Olgo89gIyMEPgeKn7UT3x9BN7LVOZV0f6f4vCHIiSwV40HbM0j/LpyShbfg0flgRZPWEcqdqadbSzJ+suNSwfUYmhVSnXvTsyRnbsrs4eI5ajRqNdvdFfdbL8WvUwUzEsUkYshdPauANC5kW6RvbNTI1OajPYnzHqs59sTsxcA5ozCh2iXTdYlbnn8i6P2xdXObyhUekP/uCCKJAkSOkN/2KjwvHyMWD9tJfwXG1Yq/dmGYzT9i61YJH6xkdgvOXCa/iUHzhAvfr4LjLvmfjyZAQe97SuEDBRSlhmyRFPqmS6psoaGRSCLKgk0VAa1v/cQ3xIvnzz6X3/y5POQaoTb5E8fPyEM62xD1MeRDw+77UPk571gfNPCKn9sBuD6H2UnwhfGWyLRee++/RfssuZ3"
            )
        )
        with patch.object(
            extraction.pytesseract, "image_to_string", return_value="Product name;Unit;Price\nFlour;kg;60"
        ) as ocr:
            self.assert_domains("price.pdf", content, ["Flour"], expected_units=["kg"])
            self.assertEqual(ocr.call_count, 3)

    def test_csv_snake_case_headers(self):
        self.assert_domains("price.csv", b"product_name;unit;price\nFlour;kg;60", ["Flour"], expected_units=["kg"])
