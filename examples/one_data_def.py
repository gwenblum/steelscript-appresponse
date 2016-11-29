# Copyright (c) 2016 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the MIT License
# accompanying the software ("License").  This software is distributed "AS IS"
# as set forth in the License.


from steelscript.appresponse.core.types import TimeFilter
from steelscript.common.service import UserAuth
from steelscript.appresponse.core.appresponse import AppResponse
from steelscript.appresponse.core.types import Key, Value
from steelscript.appresponse.core.reports import DataDef

hostname = '<hostname>'
auth = UserAuth(username='admin', password='admin')

arx = AppResponse(host=hostname, auth=auth)

job = arx.get_capture_job_by_name('<Job Name>')

tf = TimeFilter.parse_range('last 5 minutes')

columns = [Key('start_time'), Value('sum_traffic.total_bytes')]

data_def = DataDef(job=job, columns=columns,
                   granularity='1', timefilter=tf)

report = arx.create_report(data_def)

report.get_data()