# Third-party notices

Lifeboat Data Recovery includes or is built with the following third-party work.

## LZFSE and LZVN decoding

`lifeboat/fs/applecomp.py` decodes Apple's LZFSE and LZVN compression (used by macOS for
compressed files on HFS+ and APFS) following Apple's open-source reference implementation,
https://github.com/lzfse/lzfse.

```
Copyright (c) 2015-2016, Apple Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1.  Redistributions of source code must retain the above copyright notice,
    this list of conditions and the following disclaimer.

2.  Redistributions in binary form must reproduce the above copyright notice,
    this list of conditions and the following disclaimer in the documentation
    and/or other materials provided with the distribution.

3.  Neither the name of the copyright holder(s) nor the names of any contributors
    may be used to endorse or promote products derived from this software without
    specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY
EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT
SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY
OF SUCH DAMAGE.
```

## cryptography and OpenSSL

Unlocking encrypted Mac (APFS) volumes uses the Python Cryptographic Authority's `cryptography`
package (https://github.com/pyca/cryptography), under the Apache License 2.0 or the BSD license,
at your choice. It includes OpenSSL (https://www.openssl.org), under the Apache License 2.0
(https://www.apache.org/licenses/LICENSE-2.0). The license files of both are inside the package
(`cryptography-*.dist-info`) in the packaged app.

## Qt for Python (PySide6)

The desktop app is built with Qt for Python (PySide6), used under the GNU Lesser General Public
License v3. See https://www.qt.io/licensing and https://doc.qt.io/qtforpython/licenses.html.

## Python

The packaged app contains the Python runtime, under the Python Software Foundation License.
See https://docs.python.org/3/license.html.
