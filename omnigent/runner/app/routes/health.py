    @app.get("/health")
    async def health() -> dict[str, str]:
        """
        Liveness probe.

        :returns: ``{"status": "ok"}``.
        """
        return {"status": "ok"}

