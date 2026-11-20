"""Keep untrusted source strings as text in downloadable workbooks."""


def escape_formula_cells(worksheet):
    for row in worksheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.lstrip().startswith(("=", "+", "-", "@")):
                cell.data_type = "s"
                cell.number_format = "@"
