# Academic providers

Checked against official documentation on 2026-11-24. Live discovery runs only
after an explicit `discover`, `bootstrap run`, `bootstrap resume`, `weekly run`
or legacy scheduled command. Configuration validation, dry-run and run-offline
never call academic services. Query words and credentials remain in local
user configuration and environment variables, outside the public repository.

| Provider | Official reference | Endpoint | Pagination | Incremental field | Default limit |
| --- | --- | --- | --- | --- | --- |
| OpenAlex | https://help.openalex.org/api/ | /works | cursor, at most 100/page | publication date window and reconciliation on free access; complete updated-date sync requires a paid plan | 1 request/s |
| Crossref | https://www.crossref.org/documentation/retrieve-metadata/rest-api/ | /works and /works/{doi} | cursor and next-cursor; retain all original parameters | index, update or created date | public list 1 request/s |
| Europe PMC | https://europepmc.org/RestfulWebService | /rest/search | cursorMark and nextCursorMark | CREATION_DATE or UPDATE_DATE | conservative 1 request/s; official uniform rate not verified |
| PubMed | https://www.ncbi.nlm.nih.gov/books/NBK25501/ | ESearch and EFetch | retstart and retmax | edat, mdat or pdat | 2.5 requests/s without key; 8 with key, below official 3/10 ceiling |
| bioRxiv | https://api.biorxiv.org/ | details and pubs | numeric offset, details 30/page | posted-date window, local term filtering | conservative 1 request/s; official uniform rate not verified |

Crossref public and polite list rate limits were revised in July 2026:
https://community.crossref.org/t/refining-rest-api-limits-for-improved-stability-and-reliability/16137
A valid CROSSREF_MAILTO enables a conservative 2.5 requests/s polite setting.
The provider reads returned rate-limit headers and retries 429 with Retry-After.
Retry-After is honored up to a bounded delay; a longer delay returns a
retry_later error for a later run. Retryable network and HTTP errors have
finite attempts. HTTP 400 and authentication errors are not retried.
OpenAlex uses OPENALEX_API_KEY when present; anonymous searches use basic
page pagination, capped at 10,000 results, because cursor paging beyond the
first page requires a key. See the [current OpenAlex authentication guide](https://help.openalex.org/api/authentication/).
NCBI_API_KEY is optional. NCBI_TOOL and
NCBI_EMAIL are optional contact identifiers. No credential value belongs
in Topic JSON, logs, databases, screenshots or test fixtures.

Metadata from external sources is locally stored only after user invocation.
Full raw responses and PDF content are never stored by this implementation.
Abstract persistence is off by default for lower-level `discover`;
`bootstrap` and `weekly` store available abstracts in the caller-selected
private SQLite database so evidence can be reviewed. No abstract is included
in the public project or wheel.
Available provider license metadata is retained with its SourceRecord, but
it must not be assumed to grant rights to redistribute an abstract.
The [NCBI policy](https://www.ncbi.nlm.nih.gov/home/about/policies/) requires
its disclaimer and copyright notice to be evident in software using
E-utilities. NCBI does not endorse this project or warrant third-party
literature content; some PubMed abstracts belong to publishers or authors.
See [third-party notices](../THIRD_PARTY_NOTICES.md). Crossref likewise warns
that some abstracts may be copyrighted. Europe PMC and bioRxiv papers carry
item-specific licenses. The project does not redistribute their full text.
Provider coverage and timeliness are not guarantees of complete discovery.
For bioRxiv `pubs`, the preprint identity field is `preprint_doi`; the
published article DOI is `published_doi` and remains a relation rather than
an automatic identity merge. For bioRxiv details, the response message's `total` is the number of records
in the date window; `count` is the current page size. EFetch responses with
fewer PubMed articles than the preceding ESearch IDs fail the page without
advancing its checkpoint.
Private Topic extensions may set per-provider rate_per_second,
connect_timeout, read_timeout, total_timeout and max_retries. Known official
ceilings are enforced for OpenAlex, Crossref and NCBI. Europe PMC and
bioRxiv have conservative defaults because uniform ceilings were not
verified; operators must choose overrides responsibly.

`LITERATURE_RADAR_OFFLINE_TEST=1` blocks the network transport, which is used
by the synthetic CI test suite. The production CLI treats malformed records
as page failures so a checkpoint cannot silently skip them. Provider outages
are isolated; a successful source's page transactions remain committed.
