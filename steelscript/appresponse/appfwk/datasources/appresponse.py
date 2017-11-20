# Copyright (c) 2017 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the MIT License
# accompanying the software ("License").  This software is distributed "AS IS"
# as set forth in the License.

import logging
import pandas
import socket
import functools

from django import forms

from steelscript.appfwk.apps.datasource.models import \
    DatasourceTable, TableQueryBase, Column, TableField

from steelscript.appfwk.apps.datasource.forms import \
    fields_add_time_selection, DurationField

from steelscript.appfwk.apps.jobs import QueryComplete, QueryContinue

from steelscript.appfwk.apps.devices.devicemanager import DeviceManager
from steelscript.appfwk.apps.devices.forms import fields_add_device_selection
from steelscript.appfwk.libs.fields import Function
from steelscript.appfwk.apps.datasource.forms import IDChoiceField
from steelscript.appresponse.core.reports import \
    SourceProxy, DataDef, Report
from steelscript.appresponse.core.types import Key, Value, AppResponseException
from steelscript.common.timeutils import datetime_to_seconds
from steelscript.appresponse.core.fs import File
from steelscript.appfwk.apps.datasource.modules.analysis import \
    AnalysisTable, AnalysisQuery
from steelscript.appfwk.apps.datasource.models import Table
from steelscript.appfwk.apps.jobs.models import Job

logger = logging.getLogger(__name__)

APP_LABEL = 'steelscript.appresponse.appfwk'


class AppResponseColumn(Column):
    class Meta:
        proxy = True
        app_label = APP_LABEL

    COLUMN_OPTIONS = {'extractor': None}


def appresponse_source_choices(form, id_, field_kwargs, params):
    """ Query AppResponse for available capture jobs / files."""

    ar_id = form.get_field_value('appresponse_device', id_)
    if ar_id == '':
        choices = [('', '<No AppResponse Device>')]
    else:
        ar = DeviceManager.get_device(ar_id)

        choices = []

        for job in ar.capture.get_jobs():
            if job.status == 'RUNNING':
                choices.append((SourceProxy(job).path, job.name))

        if params['include_files']:
            for f in ar.fs.get_files():
                choices.append((SourceProxy(f).path, f.id))

    field_kwargs['label'] = 'Source'
    field_kwargs['choices'] = choices


def fields_add_granularity(obj, initial=None, source=None):

    if source == 'packets':
        granularities = ('0.001', '0.01', '0.1', '1', '10', '60', '600',
                         '3600', '86400')
    else:
        granularities = ('60', '600', '3600', '86400')

    field = TableField(keyword='granularity',
                       label='Granularity',
                       field_cls=DurationField,
                       field_kwargs={'choices': granularities},
                       initial=initial)
    field.save()
    obj.fields.add(field)


class AppResponseTable(DatasourceTable):
    class Meta:
        proxy = True
        app_label = APP_LABEL

    _column_class = 'AppResponseColumn'
    _query_class = 'AppResponseQuery'

    TABLE_OPTIONS = {'source': 'packets',
                     'include_files': False,
                     'show_entire_pcap': True}

    FIELD_OPTIONS = {'duration': '1h',
                     'granularity': '1m'}

    def post_process_table(self, field_options):
        # Add a time selection field

        fields_add_device_selection(self, keyword='appresponse_device',
                                    label='AppResponse', module='appresponse',
                                    enabled=True)

        if self.options.source == 'packets':
            func = Function(appresponse_source_choices, self.options)

            TableField.create(
                keyword='appresponse_source', label='Source',
                obj=self,
                field_cls=IDChoiceField,
                field_kwargs={'widget_attrs': {'class': 'form-control'}},
                parent_keywords=['appresponse_device'],
                dynamic=True,
                pre_process_func=func
            )

            if self.options.show_entire_pcap:
                TableField.create(keyword='entire_pcap', obj=self,
                                  field_cls=forms.BooleanField,
                                  label='Entire PCAP',
                                  initial=True,
                                  required=False)

        fields_add_granularity(self, initial=field_options['granularity'],
                               source=self.options.source)

        fields_add_time_selection(self, show_end=True,
                                  initial_duration=field_options['duration'])


class AppResponseQuery(TableQueryBase):

    def run(self):
        criteria = self.job.criteria

        ar = DeviceManager.get_device(criteria.appresponse_device)

        if self.table.options.source == 'packets':

            source_name = criteria.appresponse_source

            if source_name.startswith(SourceProxy.JOB_PREFIX):
                job_id = source_name.lstrip(SourceProxy.JOB_PREFIX)
                source = SourceProxy(ar.capture.get_job_by_id(job_id))
            else:
                file_id = source_name.lstrip(SourceProxy.FILE_PREFIX)
                source = SourceProxy(ar.fs.get_file_by_id(file_id))

        else:
            source = SourceProxy(name=self.table.options.source)

        col_extractors, col_names = [], {}

        for col in self.table.get_columns(synthetic=False):
            col_names[col.options.extractor] = col.name

            if col.iskey:
                col_extractors.append(Key(col.options.extractor))
            else:
                col_extractors.append(Value(col.options.extractor))

        # If the data source is of file type and entire PCAP
        # is set True, then set start end times to None

        if isinstance(source, File) and criteria.entire_pcap:
            start = None
            end = None
        else:
            start = datetime_to_seconds(criteria.starttime)
            end = datetime_to_seconds(criteria.endtime)

        data_def = DataDef(
            source=source,
            columns=col_extractors,
            granularity=str(criteria.granularity.total_seconds()),
            start=start,
            end=end)

        report = Report(ar)
        report.add(data_def)
        report.run()

        df = report.get_dataframe()
        df.columns = map(lambda x: col_names[x], df.columns)

        def to_int(x):
            return x if str(x).isdigit() else None

        def to_float(x):
            return x if str(x).replace('.', '', 1).isdigit() else None

        # Numerical columns can be returned as '#N/D' when not available
        # Thus convert them to None to help sorting
        for col in self.table.get_columns(synthetic=False):
            if col.datatype == Column.DATATYPE_FLOAT:
                df[col.name] = df[col.name].apply(lambda x: to_float(x))
            elif col.datatype == Column.DATATYPE_INTEGER:
                df[col.name] = df[col.name].apply(lambda x: to_int(x))
        return QueryComplete(df)


class AppResponseTimeSeriesTable(AnalysisTable):
    class Meta:
        proxy = True
        app_label = APP_LABEL

    _query_class = 'AppResponseTimeSeriesQuery'

    TABLE_OPTIONS = {'pivot_column_label': None,
                     'pivot_column_name': None,
                     'value_column_name': None,
                     'hide_pivot_field': False}

    def post_process_table(self, field_options):

        super(AppResponseTimeSeriesTable, self).\
            post_process_table(field_options)

        TableField.create(keyword='pivot_column_names',
                          required=not self.options.hide_pivot_field,
                          hidden=self.options.hide_pivot_field,
                          label=self.options.pivot_column_label, obj=self,
                          help_text='Name of Interested Columns '
                                    '(separated by ",")')

        self.add_column('time', 'time', datatype='time', iskey=True)


class AppResponseTimeSeriesQuery(AnalysisQuery):

    def analyze(self, jobs):
        # Based on input pivot column names, i.e. CIFS, RTP, Facebook
        # using dataframe keyed by Application ID, and start time
        # derive dataframe keyed by start_time, with each row as
        # a dictionary keyed by input pivot values

        df = jobs['base'].data()
        # First clear all the dynamic columns that were associated with
        # the table last time the report is run
        # do not delete the time column
        for col in self.table.get_columns():
            if col.name == 'time':
                continue
            col.delete()

        base_table = Table.from_ref(self.table.options.tables.base)

        time_col_name = None
        for col in base_table.get_columns():
            if col.datatype == Column.DATATYPE_TIME and col.iskey:
                time_col_name = col.name
                break

        if not time_col_name:
            raise AppResponseException("No key 'time' column defined "
                                       "in base table")

        pivot_column = self.table.options.pivot_column_name

        sub_dfs = []
        for pivot in self.job.criteria.pivot_column_names.split(','):
            # Add pivot column to the table
            pivot = pivot.strip()
            AppResponseColumn.create(self.table, pivot, pivot)

            # Add pivot column to the data frame
            sub_df = df[df[pivot_column] == pivot]
            # extract time column and value column
            sub_df = sub_df[[time_col_name,
                             self.table.options.value_column_name]]
            # Rename columns to 'time' and the pivot column name
            sub_df.rename(
                columns={time_col_name: u'time',
                         self.table.options.value_column_name: pivot},
                inplace=True
            )
            sub_dfs.append(sub_df)

        df_final = reduce(
            lambda df1, df2: pandas.merge(df1, df2, on=u'time', how='outer'),
            sub_dfs
        )

        return QueryComplete(df_final)


class AppResponseTopNTimeSeriesTable(AnalysisTable):
    class Meta:
        proxy = True
        app_label = APP_LABEL

    _query_class = 'AppResponseTopNTimeSeriesQuery'

    TABLE_OPTIONS = {'n': 10,
                     'value_column_name': None,
                     'pivot_column_name': None}

    def post_process_table(self, field_options):

        # Use criteria as the overall table uses
        # to avoid showing pivot column names field
        super(AppResponseTopNTimeSeriesTable, self).\
            post_process_table(field_options)

        # Adding key column
        self.copy_columns(self.options.related_tables.ts)


class AppResponseTopNTimeSeriesQuery(AnalysisQuery):

    def analyze(self, jobs):

        df = jobs['overall'].data()

        # First clear all the dynamic columns that were associated with
        # the table last time the report is run
        # do not delete the time column
        for col in self.table.get_columns():
            if col.name == 'time':
                continue
            col.delete()

        # Get the top N values of the value column
        val_col = self.table.options.value_column_name
        pivot_col = self.table.options.pivot_column_name
        n = self.table.options.n

        pivots = list(df.sort_values(val_col, ascending=False)
                      .head(n)[pivot_col])

        for pivot in pivots:
            # Add pivot column to the table
            AppResponseColumn.create(self.table, pivot, pivot)

        # Create an AppResponseTimeSeries Job
        self.job.criteria.pivot_column_names = ','.join(pivots)
        ts_table_ref = self.table.options.related_tables['ts']
        table = Table.from_ref(ts_table_ref)

        job = Job.create(table=table,
                         criteria=self.job.criteria,
                         update_progress=False,
                         parent=self.job)

        return QueryContinue(self.collect, jobs={'ts': job})

    def collect(self, jobs):
        df = jobs['ts'].data()
        return QueryComplete(df)


class AppResponseLinkTable(AnalysisTable):
    """This analysis table derive the hyperlink columns using the base
    table value columns.
    """
    class Meta:
        proxy = True
        app_label = APP_LABEL

    _query_class = 'AppResponseLinkQuery'

    TABLE_OPTIONS = {'pivot_column_name': None,
                     'ts_report_mod_name': None}

    def post_process_table(self, field_options):
        self.copy_columns(self.options.tables.base)


class AppResponseLinkQuery(AnalysisQuery):

    def analyze(self, jobs):

        df = jobs['base'].data()

        criteria = self.job.criteria

        devid = criteria.appresponse_device
        duration = criteria.duration.seconds
        endtime = datetime_to_seconds(criteria.endtime)
        granularity = criteria.granularity.seconds
        hostname = socket.gethostname()

        def make_report_link(mod, v):
            hostname = '127.0.0.1:30080'
            s = ('<a href="http://{}/report/appresponse/{}/?'
                 'duration={}&appresponse_device={}&endtime={}&'
                 'pivot_column_names={}&granularity={}&auto_run=true" '
                 'target="_blank">{}</a>'
                 .format(hostname, mod, duration, devid, endtime, v,
                         granularity, v))
            return s

        make_report_link_with_mod = functools.partial(
            make_report_link, self.table.options.ts_report_mod_name)

        pivot_col = self.table.options.pivot_column_name
        df[pivot_col] = df[pivot_col].map(make_report_link_with_mod)

        return QueryComplete(df)
