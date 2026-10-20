"""Explicit one-time model preparation. No import/run can silently download weights."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab_config import load_lab_config
from reranker import MODEL


def main():
    parser = argparse.ArgumentParser(description='Prepare the selected multilingual Cross-Encoder, after owner consent.')
    parser.add_argument('--allow-download', action='store_true', help='Explicit permission for this weight download only')
    args = parser.parse_args()
    config = load_lab_config(require_token=False)
    path = Path(config.reranker_model_dir)
    if not args.allow_download:
        ready = (path / 'config.json').is_file() and any((path / name).is_file() for name in ('model.safetensors','pytorch_model.bin'))
        print(json.dumps({'model': MODEL, 'local_directory': str(path), 'weights_present': ready,
            'download_performed': False}, ensure_ascii=False))
        if not ready:
            print('Weights missing. Only after owner consent, rerun with --allow-download.')
        return 0 if ready else 2
    from huggingface_hub import snapshot_download, HfApi
    files = {s.rfilename for s in HfApi().model_info(MODEL, revision=config.reranker_revision or None).siblings}
    weights = 'model.safetensors' if 'model.safetensors' in files else 'pytorch_model.bin'
    snapshot_download(MODEL, revision=config.reranker_revision or None, local_dir=path,
        allow_patterns=['config.json',weights,'tokenizer.json',
            'tokenizer_config.json','special_tokens_map.json','sentencepiece.bpe.model','vocab.txt','README.md'])
    manifest = {'model': MODEL, 'revision': config.reranker_revision, 'source': 'Hugging Face', 'download_authorized': True}
    (path / 'model-preparation.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Local Cross-Encoder prepared. No Telegram bot or paid API was started.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
