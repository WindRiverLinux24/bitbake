# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models

### WIND_RIVER_EXTENSION_BEGIN ###
class Migration(migrations.Migration):

    dependencies = [
        ('orm', '0017_distro_clone'),
    ]

    operations = [
        migrations.CreateModel(
            name='WRTemplate',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('up_id', models.IntegerField(default=None, null=True)),
                ('up_date', models.DateTimeField(default=None, null=True)),
                ('name', models.CharField(max_length=255)),
                ('description', models.CharField(max_length=255)),
                ('layer_version', models.ForeignKey(to='orm.Layer_Version', on_delete=models.CASCADE)),
            ],
        ),
        migrations.CreateModel(
            name='ProjectTemplate',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('project', models.ForeignKey(to='orm.Project', on_delete=models.CASCADE)),
                ('wrtemplate', models.ForeignKey(to='orm.WRTemplate', null=True, on_delete=models.CASCADE)),
            ],
        ),
        migrations.CreateModel(
            name='BuildTemplate',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('build', models.ForeignKey(related_name='wrtemplate_build', default=None, to='orm.Build', null=True, on_delete=models.CASCADE)),
                ('wrtemplate', models.ForeignKey(to='orm.WRTemplate', on_delete=models.CASCADE)),
            ],
        ),
    ]
### WIND_RIVER_EXTENSION_END ###

