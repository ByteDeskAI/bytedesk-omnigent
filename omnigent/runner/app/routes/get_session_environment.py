    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}")
    async def get_session_environment(
        session_id: str,
        environment_id: str,
    ) -> JSONResponse:
        """Return a single environment resource by id.

        Includes a ``metadata.root`` field on the default environment
        resource when the session has a filesystem available — the same
        root used by the filesystem API endpoints.

        :param session_id: Session/conversation identifier.
        :param environment_id: Opaque environment resource id,
            e.g. ``"default"``.
        :returns: The environment resource object.
        """
        agent_spec = await _resolve_session_agent_spec(session_id)
        resource = resource_registry.get_resource(
            session_id,
            environment_id,
        )
        if resource is None or resource.type != "environment":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Environment {environment_id!r} not found",
                    }
                },
            )
        content = session_resource_view_to_dict(resource)
        if environment_id == DEFAULT_ENVIRONMENT_ID:
            root = resource_registry.compute_default_env_root(session_id, agent_spec)
            if root is not None:
                metadata = {**content.get("metadata", {}), "root": root}
                # Expose the runner's home dir so the Web UI can expand a
                # leading ``~`` in paths the agent mentions (e.g.
                # ``~/proj/foo.md``) and resolve them against ``root`` —
                # the agent's tools run in this same runner process, so
                # this is exactly the home its ``~`` expands to. Omitted
                # when ``expanduser`` can't resolve ``~`` to an absolute
                # path (it leaves ``~`` literal — e.g. no HOME and no
                # passwd entry to fall back to).
                home = os.path.expanduser("~")
                if os.path.isabs(home):
                    metadata["home"] = home
                content = {**content, "metadata": metadata}
        return JSONResponse(
            status_code=200,
            content=content,
        )

