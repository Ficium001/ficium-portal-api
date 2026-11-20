# vendor/

Packages installed from this folder instead of an index.

| File | From | Why |
|---|---|---|
| `ficium_contract-1.9.1-py3-none-any.whl` | `Ficium001/ficium-integration`, tag `v1.9.1` (`d565f32`) | The repository is private, so the archive URL used before returned 404 to CI and to the image build. A committed wheel needs no token in CI, on Railway or on-prem, and builds offline. |

`SHA256SUMS` is checked by the Docker build (`sha256sum -c`) and by
`tests/test_vendor.py`, so a changed wheel cannot ship unnoticed.

## Updating the contract

Build the wheel reproducibly from the release tag, outside this repo:

```bash
git clone --depth 1 --branch vX.Y.Z https://github.com/Ficium001/ficium-integration /tmp/contract
cd /tmp/contract
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) pip wheel --no-deps -w /tmp/whl .
```

`SOURCE_DATE_EPOCH` fixes the timestamps inside the wheel, so anyone rebuilding
the same tag gets the same SHA-256. Then, in this repo:

1. Replace the wheel in `vendor/` and update the line in `requirements.txt`.
2. `cd vendor && sha256sum ficium_contract-*.whl > SHA256SUMS`
3. Update the table above (tag and commit).
4. Run the tests: `tests/test_integration_contract.py` exercises the contract.
