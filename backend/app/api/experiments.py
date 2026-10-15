"""HTTP representation for explicit experiment jobs."""

from ..experiment_schemas import ExperimentInput


def install_experiments(app, service):
    app.state.experiments = service

    @app.get('/api/v1/integrations/status')
    def status():
        from ..runtime_config import integration_status

        return integration_status(provider=service.provider, settings=service.settings)

    @app.get('/api/v1/experiments')
    def history():
        return service.history()

    @app.get('/api/v1/experiments/{id}')
    def detail(id: int):
        return service.snapshot(id)

    @app.post('/api/v1/experiments', status_code=202)
    async def start(data: ExperimentInput):
        return await service.start(data)

    @app.post('/api/v1/experiments/{id}/stop')
    async def stop(id: int):
        async with service.lock:
            return await service.stop(id)
