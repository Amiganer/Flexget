from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

from flexget.manager import Session
from flexget.plugins.output.history import History
from flexget.utils import json
from flexget.plugins.api.history import ObjectsContainer as OC


class TestHistoryAPI(object):
    config = "{'tasks': {}}"

    def test_history(self, api_client, schema_match):
        rsp = api_client.get('/history/')
        assert rsp.status_code == 200, 'Response code is %s' % rsp.status_code
        data = json.loads(rsp.get_data(as_text=True))

        errors = schema_match(OC.history_list_object, data)
        assert not errors

        assert data['entries'] == []

        history_entry = dict(task='test_task1', title='test_title1', url='test_url1', filename='test_filename1',
                             details='test_details1')

        with Session() as session:
            item = History()
            for key, value in history_entry.items():
                setattr(item, key, value)
            session.add(item)
            session.commit()

        rsp = api_client.get('/history/')
        assert rsp.status_code == 200, 'Response code is %s' % rsp.status_code
        data = json.loads(rsp.get_data(as_text=True))

        errors = schema_match(OC.history_list_object, data)
        assert not errors

        for key, value in history_entry.items():
            assert data['entries'][0][key] == value