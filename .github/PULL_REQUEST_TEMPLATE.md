## What this changes

<!-- One or two sentences: which GPU/driver/backend, what failed, what the fix does. -->

## Hardware validated

<!-- Paste the "Validation summary" printed by `media_converterr.py validate` (no hostnames, paths or personal data). -->

```
paste the summary here
```

- GPU (model and PCI id):
- Driver / runtime (Intel media driver, Mesa, NVIDIA driver, AMF):
- ffmpeg version (from the report):
- Kernel version:

## Checklist

- [ ] `python -m unittest discover -p "test_*.py"` passes (in the image)
- [ ] `media_converterr.py validate` passes on the hardware above, or the failures that remain are listed and explained
- [ ] The Intel QSV snapshot test (`testdata/build_cmd_qsv.json`) still passes, or the change to it is deliberate and explained
- [ ] README "Validated hardware" table updated with the row(s) the validation printed
- [ ] `AGENTS.md` updated where behaviour, verification or known issues changed
- [ ] No media files, sample paths, hostnames, API keys or personal configuration in the diff
