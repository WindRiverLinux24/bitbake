#
# BitBake Toaster Implementation
#
# Copyright (C) 2014        Intel Corporation
#
# SPDX-License-Identifier: GPL-2.0-only
#

import os
import re
import shutil
import time
from bldcontrol.models import BuildEnvironment, BuildRequest, Build
from orm.models import CustomImageRecipe, Layer, Layer_Version, Project, ToasterSetting
from orm.models import signal_runbuilds
import subprocess

from toastermain import settings

from bldcontrol.bbcontroller import BuildEnvironmentController, ShellCmdException, BuildSetupException

import logging
logger = logging.getLogger("toaster")

install_dir = os.environ.get('TOASTER_DIR')

from pprint import pformat

class LocalhostBEController(BuildEnvironmentController):
    """ Implementation of the BuildEnvironmentController for the localhost;
        this controller manages the default build directory,
        the server setup and system start and stop for the localhost-type build environment

    """

    def __init__(self, be):
        super(LocalhostBEController, self).__init__(be)
        self.pokydirname = None
        self.islayerset = False

    def _shellcmd(self, command, cwd=None, nowait=False,env=None):
        if cwd is None:
            cwd = self.be.sourcedir
        if env is None:
            env=os.environ.copy()

        logger.debug("lbc_shellcmd: (%s) %s" % (cwd, command))
        p = subprocess.Popen(command, cwd = cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if nowait:
            return
        (out,err) = p.communicate()
        p.wait()
        if p.returncode:
            if len(err) == 0:
                err = "command: %s \n%s" % (command, out)
            else:
                err = "command: %s \n%s" % (command, err)
            logger.warning("localhostbecontroller: shellcmd error %s" % err)
            raise ShellCmdException(err)
        else:
            logger.debug("localhostbecontroller: shellcmd success")
            return out.decode('utf-8')

    def getGitCloneDirectory(self, url, branch):
        """Construct unique clone directory name out of url and branch."""
        if branch != "HEAD":
            return "_toaster_clones/_%s_%s" % (re.sub('[:/@+%]', '_', url), branch)

        # word of attention; this is a localhost-specific issue; only on the localhost we expect to have "HEAD" releases
        # which _ALWAYS_ means the current poky checkout
        from os.path import dirname as DN
        local_checkout_path = DN(DN(DN(DN(DN(os.path.abspath(__file__))))))
        #logger.debug("localhostbecontroller: using HEAD checkout in %s" % local_checkout_path)
        return local_checkout_path

    ### WIND_RIVER_EXTENSION_BEGIN ###
    def proccessSetupLayerXml(self, name, dirpath, giturl, commit, localdirname,git_env):
        repo_xml=os.path.join(ToasterSetting.objects.get(name = 'SETUP_XMLDIR').value,name+'.xml')
        logger.debug("proccessSetupLayerXml: looking for setup xml %s" % repo_xml)
        xml_remotes={}
        xml_remotes['base']=ToasterSetting.objects.get(name = 'SETUP_GITURL').value
        if ToasterSetting.objects.filter(name='SETUP_PATH_FILTER').count() == 1:
            xml_path_filter=ToasterSetting.objects.get(name = 'SETUP_PATH_FILTER').value
        else:
            xml_path_filter=''

        if os.path.exists(repo_xml):
            logger.debug("proccessSetupLayerXml: processing setup xml %s" % repo_xml)
            import xml.etree.ElementTree
            with open(repo_xml,"r") as logfile:
                for line in logfile:
                    if 0 == len(line.strip()):
                        continue
                    e = xml.etree.ElementTree.XML(line)
                    xml_name=e.get('name')
                    xml_remote=os.path.join(xml_remotes[e.get('remote')],xml_name)
                    xml_path=e.get('path')
                    if xml_path_filter:
                        # substitution on xml path: 's|<regex>|xyz|'
                        if xml_path_filter.startswith('s'):
                            filter_params=xml_path_filter.split(xml_path_filter[1])
                            xml_path=re.sub(filter_params[1],filter_params[2], xml_path)
                            if xml_path.startswith('/'):
                                xml_path=xml_path[1:]
                    xml_path=os.path.join(localdirname,xml_path)
                    xml_bare=e.get('bare')
                    # clone and insert the sub-layer repo
                    if not os.path.exists(xml_path):
                        if "True" == xml_bare:
                            self._shellcmd('git clone --bare "%s" "%s"' % (xml_remote, xml_path),env=git_env)
                        else:
                            self._shellcmd('git clone "%s" "%s"' % (xml_remote, xml_path),env=git_env)
                            ref = commit if re.match('^[a-fA-F0-9]+$', commit) else 'origin/%s' % commit
                            try:
                                self._shellcmd('git fetch --all && git reset --hard "%s"' % ref, xml_path,env=git_env)
                            except:
                                logger.debug("localhostbecontroller: XML Warning commit %s not present in repo '%s'" % (commit, name))
    ### WIND_RIVER_EXTENSION_END ###

    def setCloneStatus(self,bitbake,status,total,current,repo_name):
        bitbake.req.build.repos_cloned=current
        bitbake.req.build.repos_to_clone=total
        bitbake.req.build.progress_item=repo_name
        bitbake.req.build.save()

    def setLayers(self, bitbake, layers, targets):
        """ a word of attention: by convention, the first layer for any build will be poky! """

        assert self.be.sourcedir is not None

        layerlist = []
        nongitlayerlist = []
        layer_index = 0
        git_env = os.environ.copy()
        # (note: add custom environment settings here)

        ### WIND_RIVER_EXTENSION_BEGIN ###
        # append anspass environment if present
        toaster_anspass_data=os.path.join(self.be.sourcedir,'.toaster_anspass')
        if os.path.exists(toaster_anspass_data):
            with open(toaster_anspass_data,"r") as anspassfile:
                for line in anspassfile:
                    name,value = line.strip().split('=')
                    git_env[name]=value
        ### WIND_RIVER_EXTENSION_END ###

        # set layers in the layersource

        # 1. get a list of repos with branches, and map dirpaths for each layer
        gitrepos = {}

        # if we're using a remotely fetched version of bitbake add its git
        # details to the list of repos to clone
        if bitbake.giturl and bitbake.commit:
            gitrepos[(bitbake.giturl, bitbake.commit)] = []
            gitrepos[(bitbake.giturl, bitbake.commit)].append(
                ("bitbake", bitbake.dirpath, 0))

        for layer in layers:
            # We don't need to git clone the layer for the CustomImageRecipe
            # as it's generated by us layer on if needed
            if CustomImageRecipe.LAYER_NAME in layer.name:
                continue

            # If we have local layers then we don't need clone them
            # For local layers giturl will be empty
            if not layer.giturl:
                nongitlayerlist.append( "%03d:%s" % (layer_index,layer.local_source_dir) )
                continue

            if not (layer.giturl, layer.commit) in gitrepos:
                gitrepos[(layer.giturl, layer.commit)] = []
            gitrepos[(layer.giturl, layer.commit)].append( (layer.name,layer.dirpath,layer_index) )
            layer_index += 1


        logger.debug("localhostbecontroller, our git repos are %s" % pformat(gitrepos))


        # 2. Note for future use if the current source directory is a
        # checked-out git repos that could match a layer's vcs_url and therefore
        # be used to speed up cloning (rather than fetching it again).

        cached_layers = {}

        try:
            for remotes in self._shellcmd("git remote -v", self.be.sourcedir,env=git_env).split("\n"):
                try:
                    remote = remotes.split("\t")[1].split(" ")[0]
                    if remote not in cached_layers:
                        cached_layers[remote] = self.be.sourcedir
                except IndexError:
                    pass
        except ShellCmdException:
            # ignore any errors in collecting git remotes this is an optional
            # step
            pass

        logger.info("Using pre-checked out source for layer %s", cached_layers)

        # 3. checkout the repositories
        clone_count=0
        clone_total=len(gitrepos.keys())
        self.setCloneStatus(bitbake,'Started',clone_total,clone_count,'')
        for giturl, commit in gitrepos.keys():
            self.setCloneStatus(bitbake,'progress',clone_total,clone_count,gitrepos[(giturl, commit)][0][0])
            clone_count += 1

            localdirname = os.path.join(self.be.sourcedir, self.getGitCloneDirectory(giturl, commit))
            logger.debug("localhostbecontroller: giturl %s:%s checking out in current directory %s" % (giturl, commit, localdirname))

            # see if our directory is a git repository
            if os.path.exists(localdirname):
                try:
                    localremotes = self._shellcmd("git remote -v",
                                                  localdirname,env=git_env)
                    # NOTE: this nice-to-have check breaks when using git remaping to get past firewall
                    #       Re-enable later with .gitconfig remapping checks
                    #if not giturl in localremotes and commit != 'HEAD':
                    #    raise BuildSetupException("Existing git repository at %s, but with different remotes ('%s', expected '%s'). Toaster will not continue out of fear of damaging something." % (localdirname, ", ".join(localremotes.split("\n")), giturl))
                    pass
                except ShellCmdException:
                    # our localdirname might not be a git repository
                    #- that's fine
                    pass
            else:
                if giturl in cached_layers:
                    logger.debug("localhostbecontroller git-copying %s to %s" % (cached_layers[giturl], localdirname))
                    self._shellcmd("git clone \"%s\" \"%s\"" % (cached_layers[giturl], localdirname),env=git_env)
                    self._shellcmd("git remote remove origin", localdirname,env=git_env)
                    self._shellcmd("git remote add origin \"%s\"" % giturl, localdirname,env=git_env)
                else:
                    logger.debug("localhostbecontroller: cloning %s in %s" % (giturl, localdirname))
                    self._shellcmd('git clone "%s" "%s"' % (giturl, localdirname),env=git_env)

            # branch magic name "HEAD" will inhibit checkout
            if commit != "HEAD":
                logger.debug("localhostbecontroller: checking out commit %s to %s " % (commit, localdirname))
                ref = commit if re.match('^[a-fA-F0-9]+$', commit) else 'origin/%s' % commit
                self._shellcmd('git fetch && git reset --hard "%s"' % ref, localdirname,env=git_env)

            # take the localdirname as poky dir if we can find the oe-init-build-env
            if self.pokydirname is None and os.path.exists(os.path.join(localdirname, "oe-init-build-env")):
                logger.debug("localhostbecontroller: selected poky dir name %s" % localdirname)
                self.pokydirname = localdirname

                # make sure we have a working bitbake
                if not os.path.exists(os.path.join(self.pokydirname, 'bitbake')):
                    logger.debug("localhostbecontroller: checking bitbake into the poky dirname %s " % self.pokydirname)
                    self._shellcmd("git clone -b \"%s\" \"%s\" \"%s\" " % (bitbake.commit, bitbake.giturl, os.path.join(self.pokydirname, 'bitbake')),env=git_env)

            # verify our repositories
            for name, dirpath, index in gitrepos[(giturl, commit)]:
                localdirpath = os.path.join(localdirname, dirpath)
                logger.debug("localhostbecontroller: localdirpath expects '%s'" % localdirpath)
                if not os.path.exists(localdirpath):
                    raise BuildSetupException("Cannot find layer git path '%s' in checked out repository '%s:%s'. Exiting." % (localdirpath, giturl, commit))

                if name != "bitbake":
                    layerlist.append("%03d:%s" % (index,localdirpath.rstrip("/")))

            ### WIND_RIVER_EXTENSION_BEGIN ###
            # process XML layer extensions
            for name, dirpath, index in gitrepos[(giturl, commit)]:
                self.proccessSetupLayerXml(name, dirpath, giturl, commit, localdirname, git_env)
            ### WIND_RIVER_EXTENSION_END ###

        ### WIND_RIVER_EXTENSION_BEGIN ###
        # copy Wind River specific sample conf files
        logger.debug("localhostbecontroller: prepare WR-specific bitbake sample conf files <%s><%s>" % (self.pokydirname,self.pokydirname[0:1]))
        self._shellcmd("mkdir -p %s" % os.path.join(self.pokydirname, 'config'),env=git_env)
        self._shellcmd("cp %s/config/*.sample %s" % (install_dir,os.path.join(self.pokydirname, 'config')),env=git_env)
        self._shellcmd("cp %s/.templateconf %s" % (install_dir,self.pokydirname),env=git_env)
        ### WIND_RIVER_EXTENSION_END ###

        self.setCloneStatus(bitbake,'complete',clone_total,clone_count,'')
        logger.debug("localhostbecontroller: current layer list %s " % pformat(layerlist))

        # Resolve self.pokydirname if not resolved yet, consider the scenario
        # where all layers are local, that's the else clause
        if self.pokydirname is None:
            if os.path.exists(os.path.join(self.be.sourcedir, "oe-init-build-env")):
                logger.debug("localhostbecontroller: selected poky dir name %s" % self.be.sourcedir)
                self.pokydirname = self.be.sourcedir
            else:
                # Alternatively, scan local layers for relative "oe-init-build-env" location
                for layer in layers:
                    if os.path.exists(os.path.join(layer.layer_version.layer.local_source_dir,"..","oe-init-build-env")):
                        logger.debug("localhostbecontroller, setting pokydirname to %s" % (layer.layer_version.layer.local_source_dir))
                        self.pokydirname = os.path.join(layer.layer_version.layer.local_source_dir,"..")
                        break
                else:
                    logger.error("pokydirname is not set, you will run into trouble!")

        # 5. create custom layer and add custom recipes to it
        for target in targets:
            try:
                customrecipe = CustomImageRecipe.objects.get(
                    name=target.target,
                    project=bitbake.req.project)

                custom_layer_path = self.setup_custom_image_recipe(
                    customrecipe, layers)

                if os.path.isdir(custom_layer_path):
                    layerlist.append("%03d:%s" % (layer_index,custom_layer_path))

            except CustomImageRecipe.DoesNotExist:
                continue  # not a custom recipe, skip

        layerlist.extend(nongitlayerlist)
        logger.debug("\n\nset layers gives this list %s" % pformat(layerlist))
        self.islayerset = True

        # restore the order of layer list for bblayers.conf
        layerlist.sort()
        sorted_layerlist = [l[4:] for l in layerlist]
        return sorted_layerlist

    def setup_custom_image_recipe(self, customrecipe, layers):
        """ Set up toaster-custom-images layer and recipe files """
        layerpath = os.path.join(self.be.builddir,
                                 CustomImageRecipe.LAYER_NAME)

        # create directory structure
        for name in ("conf", "recipes"):
            path = os.path.join(layerpath, name)
            if not os.path.isdir(path):
                os.makedirs(path)

        # create layer.conf
        config = os.path.join(layerpath, "conf", "layer.conf")
        if not os.path.isfile(config):
            with open(config, "w") as conf:
                conf.write('BBPATH .= ":${LAYERDIR}"\nBBFILES += "${LAYERDIR}/recipes/*.bb"\n')

        # Update the Layer_Version dirpath that has our base_recipe in
        # to be able to read the base recipe to then  generate the
        # custom recipe.
        br_layer_base_recipe = layers.get(
            layer_version=customrecipe.base_recipe.layer_version)

        # If the layer is one that we've cloned we know where it lives
        if br_layer_base_recipe.giturl and br_layer_base_recipe.commit:
            layer_path = self.getGitCloneDirectory(
                br_layer_base_recipe.giturl,
                br_layer_base_recipe.commit)
        # Otherwise it's a local layer
        elif br_layer_base_recipe.local_source_dir:
            layer_path = br_layer_base_recipe.local_source_dir
        else:
            logger.error("Unable to workout the dir path for the custom"
                         " image recipe")

        br_layer_base_dirpath = os.path.join(
            self.be.sourcedir,
            layer_path,
            customrecipe.base_recipe.layer_version.dirpath)

        customrecipe.base_recipe.layer_version.dirpath = br_layer_base_dirpath

        customrecipe.base_recipe.layer_version.save()

        # create recipe
        recipe_path = os.path.join(layerpath, "recipes", "%s.bb" %
                                   customrecipe.name)
        with open(recipe_path, "w") as recipef:
            recipef.write(customrecipe.generate_recipe_file_contents())

        # Update the layer and recipe objects
        customrecipe.layer_version.dirpath = layerpath
        customrecipe.layer_version.layer.local_source_dir = layerpath
        customrecipe.layer_version.layer.save()
        customrecipe.layer_version.save()

        customrecipe.file_path = recipe_path
        customrecipe.save()

        return layerpath


    def readServerLogFile(self):
        return open(os.path.join(self.be.builddir, "toaster_server.log"), "r").read()


    def triggerBuild(self, bitbake, layers, variables, targets, brbe):
        layers = self.setLayers(bitbake, layers, targets)
        is_merged_attr = bitbake.req.project.merged_attr

        git_env = os.environ.copy()
        # (note: add custom environment settings here)
        try:
            # insure that the project init/build uses the selected bitbake, and not Toaster's
            del git_env['TEMPLATECONF']
            del git_env['BBBASEDIR']
            del git_env['BUILDDIR']
        except KeyError:
            pass

        # init build environment from the clone
        if bitbake.req.project.builddir:
            builddir = bitbake.req.project.builddir
        else:
            builddir = '%s-toaster-%d' % (self.be.builddir, bitbake.req.project.id)
        oe_init = os.path.join(self.pokydirname, 'oe-init-build-env')
        # init build environment
        try:
            custom_script = ToasterSetting.objects.get(name="CUSTOM_BUILD_INIT_SCRIPT").value
            custom_script = custom_script.replace("%BUILDDIR%" ,builddir)
            self._shellcmd("bash -c 'source %s'" % (custom_script),env=git_env)
        except ToasterSetting.DoesNotExist:
            self._shellcmd("bash -c 'source %s %s'" % (oe_init, builddir),
                       self.be.sourcedir,env=git_env)

        # update bblayers.conf
        if not is_merged_attr:
            bblconfpath = os.path.join(builddir, "conf/toaster-bblayers.conf")
            with open(bblconfpath, 'w') as bblayers:
                bblayers.write('# line added by toaster build control\n'
                               'BBLAYERS = "%s"' % ' '.join(layers))

            # write configuration file
            confpath = os.path.join(builddir, 'conf/toaster.conf')
            with open(confpath, 'w') as conf:
                for var in variables:
                    conf.write('%s="%s"\n' % (var.name, var.value))
                conf.write('INHERIT+="toaster buildhistory"')
        else:
            # Append the Toaster-specific values directly to the bblayers.conf
            bblconfpath = os.path.join(builddir, "conf/bblayers.conf")
            bblconfpath_save = os.path.join(builddir, "conf/bblayers.conf.save")
            shutil.copyfile(bblconfpath, bblconfpath_save)
            with open(bblconfpath) as bblayers:
                content = bblayers.readlines()
            do_write = True
            was_toaster = False
            with open(bblconfpath,'w') as bblayers:
                for line in content:
                    #line = line.strip('\n')
                    if 'TOASTER_CONFIG_PROLOG' in line:
                        do_write = False
                        was_toaster = True
                    elif 'TOASTER_CONFIG_EPILOG' in line:
                        do_write = True
                    elif do_write:
                        bblayers.write(line)
                if not was_toaster:
                    bblayers.write('\n')
                bblayers.write('#=== TOASTER_CONFIG_PROLOG ===\n')
                bblayers.write('BBLAYERS = "\\\n')
                for layer in layers:
                    bblayers.write('  %s \\\n' % layer)
                ### WIND_RIVER_EXTENSION_BEGIN ###
                bblayers.write('  %s \\\n' % os.path.join(install_dir, 'layers/local'))
                ### WIND_RIVER_EXTENSION_END ###
                bblayers.write('  "\n')
                bblayers.write('#=== TOASTER_CONFIG_EPILOG ===\n')
            # Append the Toaster-specific values directly to the local.conf
            bbconfpath = os.path.join(builddir, "conf/local.conf")
            bbconfpath_save = os.path.join(builddir, "conf/local.conf.save")
            shutil.copyfile(bbconfpath, bbconfpath_save)
            with open(bbconfpath) as f:
                content = f.readlines()
            do_write = True
            was_toaster = False
            with open(bbconfpath,'w') as conf:
                for line in content:
                    #line = line.strip('\n')
                    if 'TOASTER_CONFIG_PROLOG' in line:
                        do_write = False
                        was_toaster = True
                    elif 'TOASTER_CONFIG_EPILOG' in line:
                        do_write = True
                    elif do_write:
                        conf.write(line)
                if not was_toaster:
                    conf.write('\n')
                conf.write('#=== TOASTER_CONFIG_PROLOG ===\n')
                for var in variables:
                    if (not var.name.startswith("INTERNAL_")) and (not var.name == "BBLAYERS"):
                        conf.write('%s="%s"\n' % (var.name, var.value))
                conf.write('#=== TOASTER_CONFIG_EPILOG ===\n')

        # If 'target' is just the project preparation target, then we are done
        for target in targets:
            if "_PROJECT_PREPARE_" == target.target:
                logger.debug('localhostbecontroller: Project has been prepared. Done.')
                # Update the Build Request and release the build environment
                bitbake.req.state = BuildRequest.REQ_COMPLETED
                bitbake.req.save()
                self.be.lock = BuildEnvironment.LOCK_FREE
                self.be.save()
                # Close the project build and progress bar
                bitbake.req.build.outcome = Build.SUCCEEDED
                bitbake.req.build.save()
                # Update the project status
                bitbake.req.project.set_variable(Project.PROJECT_SPECIFIC_STATUS,Project.PROJECT_SPECIFIC_CLONING_SUCCESS)
                signal_runbuilds()
                return

        # clean the Toaster to build environment
        env_clean = 'unset BBPATH;' # clean BBPATH for <= YP-2.4.0

        # run bitbake server from the clone if available
        # otherwise pick it from the PATH
        bitbake = os.path.join(self.pokydirname, 'bitbake', 'bin', 'bitbake')
        if not os.path.exists(bitbake):
            logger.info("Bitbake not available under %s, will try to use it from PATH" %
                        self.pokydirname)
            for path in os.environ["PATH"].split(os.pathsep):
                if os.path.exists(os.path.join(path, 'bitbake')):
                    bitbake = os.path.join(path, 'bitbake')
                    break
            else:
                logger.error("Looks like Bitbake is not available, please fix your environment")

        toasterlayers = os.path.join(builddir,"conf/toaster-bblayers.conf")
        if not is_merged_attr:
            self._shellcmd('%s bash -c \"source %s %s; BITBAKE_UI="knotty" %s --read %s --read %s '
                           '--server-only -B 0.0.0.0:0\"' % (env_clean, oe_init,
                           builddir, bitbake, confpath, toasterlayers), self.be.sourcedir)
        else:
            self._shellcmd('%s bash -c \"source %s %s; BITBAKE_UI="knotty" %s '
                           '--server-only -B 0.0.0.0:0\"' % (env_clean, oe_init,
                           builddir, bitbake), self.be.sourcedir)

        # read port number from bitbake.lock
        self.be.bbport = -1
        bblock = os.path.join(builddir, 'bitbake.lock')
        # allow 10 seconds for bb lock file to appear but also be populated
        for lock_check in range(10):
            if not os.path.exists(bblock):
                logger.debug("localhostbecontroller: waiting for bblock file to appear")
                time.sleep(1)
                continue
            if 10 < os.stat(bblock).st_size:
                break
            logger.debug("localhostbecontroller: waiting for bblock content to appear")
            time.sleep(1)
        else:
            raise BuildSetupException("Cannot find bitbake server lock file '%s'. Exiting." % bblock)

        with open(bblock) as fplock:
            for line in fplock:
                if ":" in line:
                    self.be.bbport = line.split(":")[-1].strip()
                    logger.debug("localhostbecontroller: bitbake port %s", self.be.bbport)
                    break

        if -1 == self.be.bbport:
            raise BuildSetupException("localhostbecontroller: can't read bitbake port from %s" % bblock)

        self.be.bbaddress = "localhost"
        self.be.bbstate = BuildEnvironment.SERVER_STARTED
        self.be.lock = BuildEnvironment.LOCK_RUNNING
        self.be.save()

        bbtargets = ''
        for target in targets:
            task = target.task
            if task:
                if not task.startswith('do_'):
                    task = 'do_' + task
                task = ':%s' % task
            bbtargets += '%s%s ' % (target.target, task)

        # run build with local bitbake. stop the server after the build.
        log = os.path.join(builddir, 'toaster_ui.log')
        local_bitbake = os.path.join(os.path.dirname(os.getenv('BBBASEDIR')),
                                     'bitbake')
        if not is_merged_attr:
            self._shellcmd(['%s bash -c \"(TOASTER_BRBE="%s" BBSERVER="0.0.0.0:%s" '
                        '%s %s -u toasterui  --read %s --read %s --token="" >>%s 2>&1;'
                        'BITBAKE_UI="knotty" BBSERVER=0.0.0.0:%s %s -m)&\"' \
                        % (env_clean, brbe, self.be.bbport, local_bitbake, bbtargets, confpath, toasterlayers, log,
                        self.be.bbport, bitbake,)],
                        builddir, nowait=True)
        else:
            self._shellcmd(['%s bash -c \"(TOASTER_BRBE="%s" BBSERVER="0.0.0.0:%s" '
                        '%s %s -u toasterui  --token="" >>%s 2>&1;'
                        'BITBAKE_UI="knotty" BBSERVER=0.0.0.0:%s %s -m)&\"' \
                        % (env_clean, brbe, self.be.bbport, local_bitbake, bbtargets, log,
                        self.be.bbport, bitbake,)],
                        builddir, nowait=True)

        logger.debug('localhostbecontroller: Build launched, exiting. '
                     'Follow build logs at %s' % log)
