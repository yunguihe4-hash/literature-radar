# Third-party notices

The original AI_OS provenance record identifies these pinned upstream projects as influences on its literature adapters. In particular, the OpenAlex inverted-index reconstruction and cursor pagination, and the Europe PMC cursor termination and HTTP-200 error recognition, are documented there as adapted implementations. This notice preserves their copyright and permission text alongside Literature Radar's MIT license.

Provenance reference: the original AI_OS Git commit `ab5a630`, file `.agents/skills/literature-radar/references/upstream.md` (development evidence; not needed at runtime). The pinned `paper-lookup` and `scholar-search` SKILL.md files each declare `license: MIT` for the adapted script areas; the repository-level licenses and those skill declarations were both checked.

## Direct runtime dependencies and Bridge host

| Component | Use | License and source |
| --- | --- | --- |
| `jsonschema` | Python Draft 2020-12 validation | [MIT](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) |
| `rfc3339-validator` | Python date-time format validation | [MIT](https://github.com/naimetti/rfc3339-validator/blob/master/LICENSE) |
| Python standard library | Core HTTP, SQLite, scheduling and file I/O | [Python Software Foundation License](https://docs.python.org/3/license.html) |
| Obsidian API | Bridge runtime host; no Obsidian code is bundled | [MIT API definitions](https://github.com/obsidianmd/obsidian-api/blob/master/LICENSE.md); the Obsidian application has separate terms |
| Node.js built-in `fs` and `path` | Bridge file access | Supplied by the Obsidian desktop host; no npm runtime package is bundled |

Transitive packages installed by `jsonschema` have their own metadata and
license files in the installed environment. The project wheel does not vendor
those packages. Build tools such as `setuptools` are not part of the runtime
wheel. The Bridge distribution consists of its own `manifest.json`,
`main.js`, and `styles.css`; it contains no private plugin `data.json` or
third-party plugin binaries.

## Provider content and terms

Literature Radar calls public metadata services but does not redistribute
their databases, paper full text, PDFs, or licensed journal rankings. Users
must follow each service's current access rules and the license of each paper:

| Service | Official usage or rights reference | Distribution boundary |
| --- | --- | --- |
| OpenAlex | [API authentication](https://help.openalex.org/api/authentication/), [API data](https://help.openalex.org/api/) | OpenAlex metadata is CC0; a paper copy or abstract may have separate rights |
| Crossref | [REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | Crossref warns that some abstracts may be copyrighted |
| Europe PMC | [Copyright](https://europepmc.org/Copyright), [REST API](https://europepmc.org/RestfulWebService) | Article licenses differ; check each article before reuse |
| PubMed/NCBI | [NCBI policies and disclaimers](https://www.ncbi.nlm.nih.gov/home/about/policies/) | NCBI does not claim copyright in publisher abstracts and does not grant blanket redistribution permission |
| bioRxiv | [API](https://api.biorxiv.org/), [about bioRxiv](https://www.biorxiv.org/about-biorxiv) | API exposes a license field; check the individual preprint |

NCBI's own disclaimer applies: NCBI does not endorse or warrant third-party
articles or software that uses its services. Some PubMed abstracts and linked
materials belong to publishers or authors. NCBI requests acknowledgment when
its public-domain materials are reused. The project does not imply NCBI
endorsement.

## Project license and bundled content

Literature Radar's first-party Core, Bridge, documentation, generic examples,
and project-owned AI_OS migrations are released under the project MIT LICENSE.
The upstream MIT notices below remain in force for adapted portions and must
be retained. No user database, feedback, trained model, private research plan,
paper PDF, or third-party plugin is included in the release.

## K-Dense scientific-agent-skills

Source: https://github.com/K-Dense-AI/scientific-agent-skills at `28f5603bc2b57ca6e2856e87af2e98686b65071f`.

```text
MIT License

Copyright (c) 2025 K-Dense Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Sebastien Dubois ai-skill-scholar

Source: https://github.com/dsebastien/ai-skill-scholar at `15ce494a761232f68610b261c9e4c8001e2c83fd`.

```text
MIT License

Copyright (c) 2026 Sebastien Dubois

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Sebastien Dubois ai-skill-arxiv

Source: https://github.com/dsebastien/ai-skill-arxiv at `bbbe55b4c91ecd0555b819e3f547a7a3f379c8f5`.

```text
MIT License

Copyright (c) 2026 Sebastien Dubois

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
