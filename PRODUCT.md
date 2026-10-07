# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Inferred from the existing local application: an individual reader collecting material for later reading and reference.

## Product Purpose

Library of Babel is a personal library for collecting and reading long-form articles, research, and security material such as CVEs. Success means a reader can save sources, organize them by topic, and return to them in a comfortable reading view.

## Positioning

One local, organized reading space for material that currently spans independent publications, research sources, and academic papers. Medium is an existing source, not the product boundary.

## Capabilities and Constraints

- Confirmed in the current implementation: topic organization, Medium discovery and search, saved article reading, offline copies, PDF output, and reader notes/highlights.
- Research papers can be added by DOI and kept on a separate shelf; their metadata (title, authors, abstract, venue, year, citation counts) is resolved on demand from Crossref. The shelf is stored with the rest of the library, including in the Telegram archive.
- Research articles are currently a UI preview; no research provider integration was requested or added.
- Preserve existing working library and reader interactions.

## Brand Commitments

- Product name: Library of Babel (selected by the user).
- The UI should represent research articles and CVEs alongside Medium articles.
- Use a simple, sleek reading-library layout with smooth, restrained interactions; long-form reading stays primary and dashboard-style decoration is out of character.

## Evidence on Hand

The existing app and its source files are the only confirmed article evidence. CVEs are fetched from external public sources when requested. No research records or provider integrations were provided; do not present invented examples or operational claims as live data.

## Product Principles

- Keep source types legible.
- Make saved material easy to organize and return to.
- Treat long-form reading as a core workflow.
- Show future source categories without implying integrations that do not exist.
