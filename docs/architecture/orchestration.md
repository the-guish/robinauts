# Orchestration

One of Robinauts foundationals premises is to be well architected to support multiple deployment modes with the same code base, and to be modular enough to be partially adopted as component on other systems. It aims to be deployable as a multi-user platform for a company, but also be fully reused as a desktop application.

[Neorc](https://github.com/open-shipyard/neorc) aims to be lightweight orchestration layer with minimal dependencies that also can be deployed to cluster or be embedded in smaller applications.


They integrate together to server multiple scenarios.

On dev mode or desktop application, Robinauts starts a local instance of Neorc that users can take for orchestrating their own local workflows.


On a cluster Neorc would be started on its own and connected to a running instance of Robinauts, creating sessions with a system user provided by Robinauts.
