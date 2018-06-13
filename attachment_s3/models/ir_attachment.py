# -*- coding: utf-8 -*-
# Copyright 2016 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)


import base64
import logging
import os
import xml.dom.minidom
from contextlib import closing, contextmanager
from functools import partial

import psycopg2

import openerp
from openerp import _, api, exceptions, fields, models
from ..s3uri import S3Uri

_logger = logging.getLogger(__name__)

try:
    import boto
    from boto.exception import S3ResponseError
except ImportError:
    boto = None  # noqa
    S3ResponseError = None  # noqa
    _logger.debug("Cannot 'import boto'.")


def clean_fs(files):
    _logger.info('cleaning old files from filestore')
    for full_path in files:
        if os.path.exists(full_path):
            try:
                os.unlink(full_path)
            except OSError:
                _logger.info(
                    "_file_delete could not unlink %s",
                    full_path, exc_info=True
                )
            except IOError:
                # Harmless and needed for race conditions
                _logger.info(
                    "_file_delete could not unlink %s",
                    full_path, exc_info=True
                )


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    # this field is in old API, we need to override the 'inverse'
    # field to modify the behavior when using S3, so we adapt
    # the calls from a new API field
    datas = fields.Binary(
        compute='_compute_datas',
        inverse='_inverse_datas',
        string='File Content',
        nodrop=True,
    )

    @api.depends('store_fname', 'db_datas')
    def _compute_datas(self):
        values = self._data_get('datas', None)
        for attach in self:
            attach.datas = values.get(attach.id)

    def _inverse_datas(self):
        # override in order to store files that need fast access,
        # we keep them in the database instead of the object storage
        location = self._storage()
        for attach in self:
            if location == 's3' and self._store_in_db_when_s3():
                # compute the fields that depend on datas
                value = attach.datas
                bin_data = value and value.decode('base64') or ''
                vals = {
                    'file_size': len(bin_data),
                    'checksum': self._compute_checksum(bin_data),
                    'db_datas': value,
                    # we seriously don't need index content on those fields
                    'index_content': False,
                    'store_fname': False,
                }
                fname = attach.store_fname
                # write as superuser, as user probably does not
                # have write access
                super(IrAttachment, attach.sudo()).write(vals)
                if fname:
                    self._file_delete(fname)
                continue
            self._data_set('datas', attach.datas, None)

    @api.multi
    def _store_in_db_when_s3(self):
        """ Return whether an attachment must be stored in db

        When we are using S3. This is sometimes required because
        the object storage is slower than the database/filesystem.

        We store image_small and image_medium from 'Binary' fields
        because they should be fast to read as they are often displayed
        in kanbans / lists. The same for web_icon_data.

        We store the assets locally as well. Not only for performance,
        but also because it improves the portability of the database:
        when assets are invalidated, they are deleted so we don't have
        an old database with attachments pointing to deleted assets.

        """
        self.ensure_one()

        # assets
        if self.res_model == 'ir.ui.view':
            # assets are stored in 'ir.ui.view'
            return True

        # Binary fields
        if self.res_field:
            # Binary fields are stored with the name of the field in
            # 'res_field'
            local_fields = ('image_small', 'image_medium', 'web_icon_data')
            # 'image' fields can be rather large and should usually
            # not be requested in bulk in lists
            if self.res_field and self.res_field in local_fields:
                return True

        return False

    @api.model
    def _get_s3_bucket(self, name=None):
        """Connect to S3 and return the bucket

        The following environment variables can be set:
        * ``AWS_HOST``
        * ``AWS_ACCESS_KEY_ID``
        * ``AWS_SECRET_ACCESS_KEY``
        * ``AWS_BUCKETNAME``

        If a name is provided, we'll read this bucket, otherwise, the bucket
        from the environment variable ``AWS_BUCKETNAME`` will be read.

        """
        host = os.environ.get('AWS_HOST')
        if host:
            connect_s3 = partial(boto.connect_s3, host=host)
        else:
            connect_s3 = boto.connect_s3

        access_key = os.environ.get('AWS_ACCESS_KEY_ID')
        secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        if name:
            bucket_name = name
        else:
            bucket_name = os.environ.get('AWS_BUCKETNAME')
        if not (access_key and secret_key and bucket_name):
            msg = _('If you want to read from the %s S3 bucket, the following '
                    'environment variables must be set:\n'
                    '* AWS_ACCESS_KEY_ID\n'
                    '* AWS_SECRET_ACCESS_KEY\n'
                    'If you want to write in the %s S3 bucket, this variable '
                    'must be set as well:\n'
                    '* AWS_BUCKETNAME\n'
                    'Optionally, the S3 host can be changed with:\n'
                    '* AWS_HOST\n'
                    ) % (bucket_name, bucket_name)

            raise exceptions.UserError(msg)

        try:
            conn = connect_s3(aws_access_key_id=access_key,
                              aws_secret_access_key=secret_key)

        except S3ResponseError as error:
            # log verbose error from s3, return short message for user
            _logger.exception('Error during connection on S3')
            raise exceptions.UserError(self._parse_s3_error(error))

        bucket = conn.lookup(bucket_name)
        if not bucket:
            bucket = conn.create_bucket(bucket_name)
        return bucket

    @staticmethod
    def _parse_s3_error(s3error):
        msg = s3error.reason
        # S3 error message is a XML message...
        doc = xml.dom.minidom.parseString(s3error.body)
        msg_node = doc.getElementsByTagName('Message')
        if msg_node:
            msg = '%s: %s' % (msg, msg_node[0].childNodes[0].data)
        return msg

    @api.model
    def _file_read_s3(self, fname, bin_size=False):
        s3uri = S3Uri(fname)
        try:
            bucket = self._get_s3_bucket(name=s3uri.bucket())
        except exceptions.UserError:
            _logger.exception(
                "error reading attachment '%s' from object storage", fname
            )
            return ''
        filekey = bucket.get_key(s3uri.item())
        if filekey:
            read = base64.b64encode(filekey.get_contents_as_string())
        else:
            read = ''
            _logger.info("attachment '%s' missing on object storage", fname)
        return read

    @api.model
    def _file_read(self, fname, bin_size=False):
        if fname.startswith('s3://'):
            return self._file_read_s3(fname, bin_size=bin_size)
        else:
            _super = super(IrAttachment, self)
            return _super._file_read(fname, bin_size=bin_size)

    @api.model
    def _file_write(self, value, checksum):
        storage = self._storage()
        if storage == 's3':
            bucket = self._get_s3_bucket()
            bin_data = value.decode('base64')
            key = self._compute_checksum(bin_data)
            filekey = bucket.get_key(key) or bucket.new_key(key)
            filename = 's3://%s/%s' % (bucket.name, key)
            try:
                filekey.set_contents_from_string(bin_data)
            except S3ResponseError as error:
                # log verbose error from s3, return short message for user
                    _logger.exception(
                        'Error during storage of the file %s' % filename
                    )
                    raise exceptions.UserError(
                        _('The file could not be stored: %s') %
                        (self._parse_s3_error(error),)
                    )
        else:
            filename = super(IrAttachment, self)._file_write(value, checksum)
        return filename

    @api.model
    def _file_delete(self, fname):
        if fname.startswith('s3://'):
            # using SQL to include files hidden through unlink or due to record
            # rules
            cr = self.env.cr
            cr.execute("SELECT COUNT(*) FROM ir_attachment "
                       "WHERE store_fname = %s", (fname,))
            count = cr.fetchone()[0]
            s3uri = S3Uri(fname)
            bucket_name = s3uri.bucket()
            item_name = s3uri.item()
            # delete the file only if it is on the current configured bucket
            # otherwise, we might delete files used on a different environment
            if bucket_name == os.environ.get('AWS_BUCKETNAME'):
                bucket = self._get_s3_bucket()
                filekey = bucket.get_key(item_name)
                if not count and filekey:
                    try:
                        filekey.delete()
                        _logger.info(
                            'file %s deleted on the object storage' % (fname,)
                        )
                    except S3ResponseError:
                        # log verbose error from s3, return short message for
                        # user
                        _logger.exception(
                            'Error during deletion of the file %s' % fname
                        )
        else:
            super(IrAttachment, self)._file_delete(fname)

    @api.multi
    def _move_attachment_to_s3(self):
        self.ensure_one()
        _logger.info('inspecting attachment %s (%d)',
                     self.name, self.id)
        fname = self.store_fname
        if fname:
            # migrating from filesystem filestore
            # or from the old 'store_fname' without the bucket name
            _logger.info('moving %s on the object storage', fname)
            self.write({'datas': self.datas,
                        # this is required otherwise the
                        # mimetype gets overriden with
                        # 'application/octet-stream'
                        # on assets
                        'mimetype': self.mimetype})
            _logger.info('moved %s on the object storage', fname)
            return self._full_path(fname)
        elif self.db_datas:
            _logger.info('moving on the object storage from database')
            self.write({'datas': self.datas})

    @api.model
    def _force_storage_s3(self, new_cr=False):
        if not self.env['res.users'].browse(self.env.uid)._is_admin():
            raise exceptions.AccessError(
                _('Only administrators can execute this action.')
            )

        storage = self._storage()
        if storage != 's3':
            return
        _logger.info('migrating files to the object storage')
        domain = ['!', ('store_fname', '=like', 's3://%'),
                  '|',
                  ('res_field', '=', False),
                  ('res_field', '!=', False)]
        # We do a copy of the environment so we can workaround the
        # cache issue below. We do not create a new cursor because
        # it causes serialization issues due to concurrent updates on
        # attachments during the installation
        with self.do_in_new_env(new_cr=new_cr) as new_env:
            attachment_model_env = new_env['ir.attachment']
            ids = attachment_model_env.search(domain).ids
            files_to_clean = []
            for attachment_id in ids:
                try:
                    with new_env.cr.savepoint():
                        # check that no other transaction has
                        # locked the row, don't send a file to S3
                        # in that case
                        self.env.cr.execute("SELECT id "
                                            "FROM ir_attachment "
                                            "WHERE id = %s "
                                            "FOR UPDATE NOWAIT",
                                            (attachment_id,),
                                            log_exceptions=False)

                        # This is a trick to avoid having the 'datas' function
                        # fields computed for every attachment on each
                        # iteration of the loop.  The former issue being that
                        # it reads the content of the file of ALL the
                        # attachments on each loop.
                        new_env.clear()
                        attachment = attachment_model_env.browse(attachment_id)
                        path = attachment._move_attachment_to_s3()
                        if path:
                            files_to_clean.append(path)
                except psycopg2.OperationalError:
                    _logger.error('Could not migrate attachment %s to S3',
                                  attachment_id)

            def clean():
                clean_fs(files_to_clean)

            # delete the files from the filesystem once we know the changes
            # have been committed in ir.attachment
            if files_to_clean:
                new_env.cr.after('commit', clean)

    @contextmanager
    def do_in_new_env(self, new_cr=False):
        """ Context manager that yields a new environment

        Using a new Odoo Environment thus a new PG transaction.
        """
        with api.Environment.manage():
            if new_cr:
                registry = openerp.modules.registry.RegistryManager.get(
                    self.env.cr.dbname
                )
                with closing(registry.cursor()) as cr:
                    try:
                        yield self.env(cr=cr)
                    except:
                        cr.rollback()
                        raise
                    else:
                        # disable pylint error because this is a valid commit,
                        # we are in a new env
                        cr.commit()  # pylint: disable=invalid-commit
            else:
                # make a copy
                yield self.env()

    @api.model
    def force_storage(self):
        storage = self._storage()
        if storage == 's3':
            self._force_storage_s3()
        else:
            return super(IrAttachment, self).force_storage()
