# -*- coding: utf-8 -*-
# Generated by Django 1.11.25 on 2019-12-09 20:39
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0008_remove_title_and_url'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='program',
            name='managing_organization',
        ),
        migrations.DeleteModel(
            name='Program',
        ),
    ]
