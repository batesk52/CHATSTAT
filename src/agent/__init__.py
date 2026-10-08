"""Embedded Claude agent foundation.

This package contains the Qt<->asyncio bridge primitives, the
no-hardware mock engine/connection pair, and the EngineAdapter that
exposes measurement and device operations to the agent tool layer.

Concurrency model (see architecture.md, "Embedded Claude Agent -
Threading/Async Bridge"): the agent runs an asyncio loop inside its own
worker QThread and touches the engine/GUI only by marshaling callables
onto the GUI thread via :mod:`src.agent.bridge`, awaiting completion
through thread-safe futures resolved by one-shot Qt signal slots.
"""
