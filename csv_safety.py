"""Keep CSV cells from being evaluated as formulas by spreadsheet applications.

Cells whose first non-blank character is a formula trigger are prefixed with a
single quote. Cells that already start with a quote are prefixed too, so that
restore_cell() can recover the original value exactly when the CSV is read back.
"""

FORMULA_TRIGGERS = ('=', '+', '-', '@', '\t', '\r', '\n', '\uff1d', '\uff0b', '\uff0d', '\uff20')


def _needs_prefix(value):
    return value[:1] in FORMULA_TRIGGERS or value[:1] == "'" or value.lstrip()[:1] in FORMULA_TRIGGERS


def neutralize_cell(value):
    if isinstance(value, str) and _needs_prefix(value):
        return "'" + value
    return value


def restore_cell(value):
    if isinstance(value, str) and value[:1] == "'" and _needs_prefix(value[1:]):
        return value[1:]
    return value


def neutralize_row(row):
    return {key: neutralize_cell(value) for key, value in row.items()}
