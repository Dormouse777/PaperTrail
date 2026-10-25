# Licensing and provenance review

The project owner selected **MIT** on 2026-10-25. The project's own code,
documentation, public skills and synthetic fixtures are covered by [LICENSE](LICENSE).
The package metadata declares the same license and includes its text in the wheel.
Third-party dependencies and external content retain their own terms.
See the [MIT terms](https://choosealicense.com/licenses/mit/) and
[GitHub's licensing guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).
Selecting MIT does not relicense upstream services, papers or model assets, and
does not by itself constitute a legal clearance certificate for third-party material.

## What is being distributed

`export-manifest.json` identifies each selected source and its exact SHA-256.
Project integration code is selected from the maintained acquisition, document,
retrieval, source/evidence and institution-record modules. The facade, offline
fixtures, tests, documentation and public skills belong to this project tree.
Folder names such as `_legacy/DIFY` mean project-written integration code, not a
copy of the upstream Dify service. File selection and lack of an attribution
header do not by themselves establish authorship or legal rights; the owner
review remains necessary.

The demonstration PDF and canonical fixture are self-authored synthetic data,
not a journal paper, scientific evidence or a real MinerU result. Original
research papers, institutional materials, browser sessions, live datasets and
model/tokenizer JSON files are excluded. No redistribution rights for those
materials are claimed.

## Dependencies and external services

| Component | Version/scope checked | License or boundary |
| --- | --- | --- |
| pypdf | 6.10.0 wheel metadata and bundled license checked | [BSD-3-Clause](https://github.com/py-pdf/pypdf/blob/6.10.0/LICENSE). Installed as a dependency; its wheel and notices retain its upstream terms |
| tokenizers | Optional 0.23.2 | [Apache-2.0](https://github.com/huggingface/tokenizers/blob/v0.23.2/LICENSE). Not included in the base wheel; tokenizer assets/models and transitive dependencies need their own provenance checks |
| RAGFlow | External service; upstream source excluded | Upstream [Apache-2.0](https://github.com/infiniflow/ragflow/blob/main/LICENSE); does not license this whole project or model assets |
| Dify | External optional service; source/volumes excluded | [Dify license](https://github.com/langgenius/dify/blob/1.17.1/LICENSE) has additional conditions to Apache-2.0; do not describe it as plain Apache or MIT |
| Zotero / MinerU / model providers | Separate operator-installed services | Not redistributed or relicensed here; deployment and asset terms must be checked separately |

The base project wheel contains project sources/resources and package metadata;
it does not bundle these third-party services or installed Python environments.
Installing optional dependencies does not grant access to proprietary content,
publisher subscriptions or paid models. Exact dependency wheels and source
hashes are retained in the private validation receipt for reproducibility; that
private receipt is not copied into the public repository.
