---
base_model: openbmb/MiniCPM5-1B
library_name: peft
license: apache-2.0
datasets:
  - liumindmind/NekoQA-30K
tags:
  - neko30k
  - nekoqa
---

# MiniCPM5-1B NekoQA v2 LoRA

Local dev copy. Public releases on Hugging Face:

- **PEFT**: [DennisHuang648/MiniCPM5-1B-NekoQA-v2-LoRA](https://huggingface.co/DennisHuang648/MiniCPM5-1B-NekoQA-v2-LoRA)
- **GGUF**: [DennisHuang648/MiniCPM5-1B-NekoQA-v2-LoRA-GGUF](https://huggingface.co/DennisHuang648/MiniCPM5-1B-NekoQA-v2-LoRA-GGUF)

## Training data

This LoRA was fine-tuned on the **neko30k** dataset (Hugging Face: [liumindmind/NekoQA-30K](https://huggingface.co/datasets/liumindmind/NekoQA-30K)), 30,834 cat-girl QA pairs.

See `USAGE.md` for details.
