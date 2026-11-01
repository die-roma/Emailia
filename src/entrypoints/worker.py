"""Emailia worker entrypoint: discovers workflows and starts the worker."""

import asyncio


import mistralai_workflows


def _discover_workflows():
    import importlib

    for module in (
        "mistralai.workflows",
        "mistralai_workflows",
        "mistralai.workflows.discovery",
    ):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        if hasattr(mod, "discover_workflows"):
            return mod.discover_workflows()
    raise RuntimeError(
        "discover_workflows() not found - check that mistralai-workflows is installed"
    )


async def main() -> None:
    discovered = _discover_workflows()
    await mistralai_workflows.run_worker(discovered)


if __name__ == "__main__":
    asyncio.run(main())
