#!/usr/bin/env python3
"""Run without installing:  ./vault-scene-tagger <media-root> [options]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from vault_scene_tagger.cli import main  # noqa: E402

sys.exit(main())
