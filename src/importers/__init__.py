"""Excel parsing and the column-mapping wizard (TZ 7, TZ 3.2).

Excel and Sheets are an import format only -- the storage is PostgreSQL (CLAUDE.md).
The parser is written and tested against `tests/fixtures/reference/Invasion.xlsx`, whose
contents never reach the product.

Owner: stage 1 (import is out of the stage 0 scope, see docs/stage0-plan.md). Empty until
then; the directory exists because TZ 3.2 fixes the repository layout.
"""
