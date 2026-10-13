# Medex Open XML SDK validator

This directory contains the implementation of the trusted validator required by
`ppt-report`. It uses Microsoft's `DocumentFormat.OpenXml.Validation.OpenXmlValidator`
with the `Office2016` file-format target. The SDK dependency is fixed at 3.3.0,
and `packages.lock.json` pins and hashes its complete NuGet dependency graph.

The program is deliberately narrow: it accepts one absolute `.pptx` path,
opens it read-only, checks conservative ZIP resource limits, and performs SDK
validation. It does not access the network, launch child processes, load
plug-ins, or modify the presentation.

An SDK pass proves Open XML schema and semantic compatibility for the selected
Office version. It does not prove that PowerPoint Online can fetch a particular
URL; HTTP method support, redirects, signed-URL encoding, expiry, range requests,
and response headers must be tested separately at the delivery boundary.

## Trusted build and installation

Build once in the trusted AGSX image pipeline with the .NET SDK version pinned
by `global.json`. Run the commands from this directory so the SDK pin is
honored. Restore is a build-time operation; the published executable is
self-contained and needs no .NET installation or network access at runtime.

For an x86-64 AGSX image:

```sh
dotnet restore Medex.OpenXmlSdkValidator.csproj --locked-mode
dotnet publish Medex.OpenXmlSdkValidator.csproj --configuration Release \
  --runtime linux-x64 --self-contained true --no-restore \
  --output artifacts/linux-x64
install -o root -g root -m 0755 \
  artifacts/linux-x64/medex-openxml-sdk-validator \
  /usr/local/bin/medex-openxml-sdk-validator
```

Use `linux-arm64` in the publish command for an ARM64 image. The locked restore
covers both supported Linux runtime identifiers. Do not copy the project, NuGet
cache, or restore credentials into the runtime image. Record the
published executable's SHA-256 in the image build provenance and keep the
installed file root-owned and non-writable by the sandbox user.

Verify the installed command with a known-valid fixture:

```sh
/usr/local/bin/medex-openxml-sdk-validator \
  --target Office2016 --file /absolute/path/fixture.pptx
```

## Command contract

The only accepted invocation is:

```text
medex-openxml-sdk-validator --target Office2016 --file /absolute/path/file.pptx
```

The two option pairs may be given in either order. Unknown or duplicate options,
relative paths, symbolic links, non-`.pptx` inputs, oversized ZIP packages, and
unsafe or duplicate ZIP entry names are rejected.

On success stdout is exactly `VALID target=Office2016 errors=0` (plus the final
newline) and stderr is empty; `generate_ppt.py` verifies this marker as well as
the zero exit status before publishing a deck.

Exit codes:

| Code | Meaning |
| ---: | --- |
| 0 | The SDK reported zero Office 2016 validation errors. |
| 2 | Invalid arguments or unsupported target. |
| 3 | Missing, inaccessible, or rejected input file. |
| 4 | Malformed package or one or more SDK validation errors. |
| 5 | Unexpected validator failure. |

At most 100 validation details are written to standard error. All errors are
still counted, and any non-zero count returns exit code 4. Stack traces and
file contents are never emitted.
