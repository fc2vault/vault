"""vault-scene-tagger — writes ``*.scenes.json`` scene-marker sidecars next to videos.

The only contract with any reader (e.g. the Vault media server) is the sidecar format in
SCENES_SPEC.md. Standard library + ffmpeg/ffprobe only.
"""

NAME = "vault-scene-tagger"
__version__ = "0.1.0"
GENERATOR = "%s %s" % (NAME, __version__)
