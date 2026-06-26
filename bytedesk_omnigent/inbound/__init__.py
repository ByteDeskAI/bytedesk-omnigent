"""Generic inbound-event ingestion pipeline (ADR-0155).

Pipes-and-Filters core that every external source flows through: translate →
wire-tap → idempotent claim → content-based fan-out. See ADR-0155 for the full
GoF + EIP pattern map.
"""
