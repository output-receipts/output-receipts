"""Output Receipts: per-grant receipts of observable research-output sharing, built from public data.

Pipeline: NIH RePORTER (grants -> linked papers) -> PMC / Europe PMC open full text
-> data/code availability statements and repository identifiers -> identifier resolution
-> per-paper disposition -> per-grant receipt.

This measures FINDABILITY of shared outputs, not compliance with any sharing plan.
"""
__version__ = "0.1.0"
