"""Built-in, generic, **redacted** starter 秘方 examples shipped in the repo (T6.4).

DESIGN §11.2C / §765: ``foreman.db`` never enters git — your real workflows / skills / standards /
QA rubrics live only in your own local DB. To let OSS users start from something, this package ships
a tiny, generic set of example definitions (no secrets, no project-specific anything). Load them into
a fresh local DB with ``foreman seed-examples`` (or :func:`foreman.client.core.examples.seed_examples`).

The example bodies are plain files under ``definitions/`` (workflows / QA rubrics as YAML, skills /
code standards as Markdown) plus a ``manifest.yaml`` listing each one's kind / name / scope — readable
as templates and loadable as data.
"""
