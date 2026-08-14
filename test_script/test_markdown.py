"""Markdown output normalization regression tests."""

from src.api.main import _repair_collapsed_markdown_tables


def test_repairs_collapsed_gfm_table_rows() -> None:
    collapsed = (
        "| 产品类别 | 2023销量 | |---|---| "
        "| Touring Bikes | 252 | | Road Bikes | 216 | | Jerseys | 212 |"
    )

    assert _repair_collapsed_markdown_tables(collapsed) == (
        "| 产品类别 | 2023销量 |\n"
        "|---|---|\n"
        "| Touring Bikes | 252 |\n"
        "| Road Bikes | 216 |\n"
        "| Jerseys | 212 |"
    )


def test_repairs_compact_double_pipe_table_from_streamed_answer() -> None:
    collapsed = (
        "| product_category | total_order_qty ||---|---:| "
        "| Touring Bikes |252| | Road Bikes |216| | Jerseys |212|"
    )

    assert _repair_collapsed_markdown_tables(collapsed) == (
        "| product_category | total_order_qty |\n"
        "|---|---:|\n"
        "| Touring Bikes |252|\n"
        "| Road Bikes |216|\n"
        "| Jerseys |212|"
    )


def test_leaves_valid_tables_prose_and_code_fences_unchanged() -> None:
    valid = "| A | B |\n|---|---|\n| 1 | 2 |"
    prose = "Use A | B for alternatives."
    fenced = "```text\n| A | B | |---|---| | 1 | 2 |\n```"

    assert _repair_collapsed_markdown_tables(valid) == valid
    assert _repair_collapsed_markdown_tables(prose) == prose
    assert _repair_collapsed_markdown_tables(fenced) == fenced


def test_repairs_rows_collapsed_after_a_correct_separator_line() -> None:
    collapsed = (
        "| top_category | qty_2023 | qty_2024 |\n"
        "|---|---|---|\n"
        "| Bikes | 515 | 168 | | Clothing | 497 | 109 | | Accessories | 298 | 37 |"
    )

    assert _repair_collapsed_markdown_tables(collapsed) == (
        "| top_category | qty_2023 | qty_2024 |\n"
        "|---|---|---|\n"
        "| Bikes | 515 | 168 |\n"
        "| Clothing | 497 | 109 |\n"
        "| Accessories | 298 | 37 |"
    )


def test_keeps_rows_that_legitimately_contain_empty_cells() -> None:
    with_empty_cells = (
        "| category | note | qty |\n"
        "|---|---|---|\n"
        "| Bikes | | 515 |\n"
        "| Clothing | | 497 |"
    )

    assert _repair_collapsed_markdown_tables(with_empty_cells) == with_empty_cells
