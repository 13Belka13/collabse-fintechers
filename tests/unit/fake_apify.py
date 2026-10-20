# A stand-in for apify_client.ApifyClient (tests only): records calls, returns a run and a dataset page.
import types


class FakeApify:
    """Stands in for apify_client.ApifyClient: records calls, returns a run and a dataset page."""

    def __init__(self, items=None, status="SUCCEEDED", error=None):
        self.items, self.status, self.error = items or [], status, error
        self.calls, self.fields = [], []

    def actor(self, actor_id):
        def call(**kw):
            self.calls.append((actor_id, kw))
            if self.error:
                raise self.error
            return types.SimpleNamespace(id="RunAbc123", status=self.status, default_dataset_id="ds1",
                                         usage_total_usd=0.0123)
        return types.SimpleNamespace(call=call)

    def run(self, run_id):
        return types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            id=run_id, status=self.status, default_dataset_id="ds1", usage_total_usd=0.0482))

    def dataset(self, _id):
        def list_items(fields=None):
            self.fields.append(fields)
            return types.SimpleNamespace(items=self.items)
        return types.SimpleNamespace(list_items=list_items)
