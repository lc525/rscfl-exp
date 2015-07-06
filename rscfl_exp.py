#!/usr/bin/env python2.7

# Resourceful experiment reproduction script
#
# Rscfl experiments typically require coordination between multiple machines
# and actions like starting/stopping services and containers/VMs.
#
# This script uses the python fabric library to automate that process so that
# minimal user interaction is needed. It can also be used for reproducing
# figures/results from previous experimental runs.
#
# Lucian Carata, 2015

import argparse
import fabric
import fabric.api
from   fabric.contrib.files import exists
from   fabric.contrib.console import confirm
from   fabric.api import settings
import paramiko
import json
import subprocess
import time
import sys
import os
import re
import io

try:
    from StringIO import StringIO
except:
    from StringIO import StringIO

# ssh debug
#import logging
#logging.basicConfig(level=logging.DEBUG)

# machine defaults. those can be changed by arguments passed to the program
# when running, so always consider the args.* versions authoritative

# machine running ab (the lighttpd load), data pre-processing and plotting
# set this to localhost if you're just reproducing paper figures
fg_load_vm = "rscfl-demo.dtg.cl.cam.ac.uk"

# machine controlling background load (nr of vms and their tasks);
# may progressively increase load if experiment requires it
bg_load_vm = "so-22-8.dtg.cl.cam.ac.uk"

# machine running lighttpd and rscfl
target_vm = "so-22-50.dtg.cl.cam.ac.uk"

# config variables
config_vars = {}
var_regexp = "%(?P<var>[a-zA-z0-9.\-_]*)%" # matches %var-name% occurences
rvar = re.compile(var_regexp)

@fabric.api.task
def create_experiment_dir(location, dirname, meta):
    i = 1
    out_path = os.path.normpath(location + "/" + dirname)
    while fabric.contrib.files.exists(out_path):
        out_path = os.path.normpath(location + "/" + dirname + "_" + str(i))
        i = i + 1
    if i > 1:
        exp_dir_name = dirname + "_" + str(i-1)
    else:
        exp_dir_name = dirname
    result_path = os.path.normpath(out_path + "/results")
    data_path = os.path.normpath(out_path + "/data")
    print("Creating experiment in %s" % fabric.api.env.host_string+":"+out_path)
    fabric.api.run("mkdir -p %s; echo '%s' > %s/meta" %
            (out_path, '{\n  \"descr\": \"' + meta + '\"', out_path), pty=True)
    fabric.api.run("mkdir -p %s" % result_path, pty=True)
    fabric.api.run("mkdir -p %s" % data_path, pty=True)
    return (out_path, result_path, data_path, exp_dir_name)

@fabric.api.task
def add_meta(location, meta):
    return fabric.api.run("echo '%s' >> %s/meta" % (meta, location), pty=True)

@fabric.api.task
def get_script_rev(script_dir):
   git_rev = fabric.api.run("git --git-dir %s/.git rev-parse --short HEAD" % script_dir,
                  pty=True)
   meta = ', \"scripts-rev\":  \"'  + git_rev  + '\"'
   return meta

@fabric.api.task
def copy_to_remote(local_file, out_file):
    fabric.operations.put(local_file, out_file)

@fabric.api.task
def copy_from_remote(remote_tc, local_tc):
    fabric.operations.get(remote_path=remote_tc, local_path=local_tc)

@fabric.api.task
def copy_file(from_file, to_file, move=False):
    cmd = "cp "
    if(move == True):
        cmd = "mv "
    cmd = cmd + from_file + " " + to_file
    return fabric.api.run(cmd, pty=True)

@fabric.api.task
def get_target_meta():
   print("Getting metadata from %s" % fabric.api.env.host_string )
   rscfl_meta = fabric.api.run("lsmod | grep rscfl | awk '{ print $1 }'", pty=True)
   uname_meta = fabric.api.run("uname -r", pty=True)
   try:
     with settings(warn_only=True):
        virt_mode = fabric.api.run("systemd-detect-virt; true", pty=True)
   except:
     virt_mode = "bare metal"

   meta = (', \"fg-virt\":  \"'  + virt_mode  + '\"\n'
           ', \"fg-uname\": \"' + uname_meta + '\"\n'
           ', \"fg-rscfl\": \"' + rscfl_meta + '\"')
   return meta

@fabric.api.task
def get_process_list():
    return fabric.api.run("ps -af | awk '{print substr($0, index($0, $8))}'", pty=True)

@fabric.api.task
def run_bg_load(cmd, outStream):
    print("Running background load on %s" % fabric.api.env.host_string)
    bg_load = fabric.api.run(cmd, pty=True, stdout=outStream)
    return bg_load

@fabric.api.task
def run_fg_load(cmd):
    print("Running foreground load on %s" % fabric.api.env.host_string)
    fg_load = fabric.api.run(cmd, pty=True)
    return fg_load

@fabric.api.task
def run_processing(cmd):
    print("Parsing experiment data on %s" % fabric.api.env.host_string)
    return fabric.api.run(cmd, pty=True)

@fabric.api.task
def run_cmd(cmd, msg):
    print(msg)
    return fabric.api.run(cmd, pty=True)

def get_experiment_load_meta(load_vm, msg):
    ps_before = fabric.api.execute(get_process_list, hosts=load_vm)
    fabric.contrib.console.confirm(msg)
    ps_after = fabric.api.execute(get_process_list, hosts=load_vm)
    diff = fabric.operations.local("sort <(echo \"%s\") <(echo \"%s\") | grep -v xl | uniq -u"
                                   % (ps_before[load_vm], ps_after[load_vm]),
                                   capture=True, shell="/bin/bash")
    return diff


def end_meta(out_path, target):
    fabric.api.execute(add_meta, out_path, "}", hosts=target)


def config_replace_vars(cfg_json, local_vars):
    def replace_match(matchobj):
        if matchobj.group('var') not in [None, '']:
            var_name = matchobj.group('var')
            if var_name.startswith("b."):
                if var_name[2:] in config_vars:
                    return config_vars[var_name[2:]]
                else:
                    print("Configuration error: unknown builtin variable %s"
                          % var_name[2:])
                    return "%%%s%%" % var_name  # return the original string
            elif var_name.startswith("l."):
                if var_name[2:] in local_vars:
                    return local_vars[var_name[2:]]
                else:
                    print("Local config var %s unknown" % var_name[2:])
                    return "%%%s%%" % var_name # return the original string
            else:
                if var_name in cfg_json:
                    return cfg_json[var_name]
                else:
                    print("Configuration error: unknown variable %s"
                          % var_name)
                    return "%%%s%%" % var_name # return the original string
    return replace_match

def config_process_vars(field, cfg_json, local_vars={}):
    return rvar.sub(config_replace_vars(cfg_json, local_vars), field)

class BgLoadScanIO(StringIO):
    def __init__(self, args, cfg_json):
        StringIO.__init__(self)
        self.args = args
        self.cfg_json = cfg_json
        self.fg_load_running = False

    def write(self, text):
        if (text.startswith("$ESTART")):
            if self.fg_load_running == False:
                fabric.api.output["stdout"] = False
                # prepare foreground command
                fg_load_cmd = config_process_vars(self.cfg_json['fg-load']['script'],
                                                  self.cfg_json)
                fg_load_cmd_esc = fg_load_cmd.replace('"', '\\\\"')
                fabric.api.execute(add_meta, config_vars['exp_dir'],
                        ", \"fg_load_cmd\": \"%s\"" % fg_load_cmd_esc,
                        hosts=self.args.fg_load_vm)
                self.fg_load_running = True

                # ready to execute foreground experiment load (typically, ab)
                fabric.api.execute(run_fg_load, fg_load_cmd,
                                   hosts=self.args.fg_load_vm)
                self.fg_load_running = False
                for aux_out in self.cfg_json['fg-load']['aux-out']:
                    aux_out_s = config_process_vars(aux_out, self.cfg_json)
                    fabric.api.execute(copy_file, aux_out_s,
                                       config_vars['result_dir'], True,
                                       hosts=self.args.fg_load_vm)
            else:
                print("A fg_load was scheduled to start but an existing load is still running")


def main():
    fabric.api.env.use_ssh_config = True
    fabric.api.env.forward_agent = True
    parser = argparse.ArgumentParser(description="(re-)produce rscfl experiments")
    parser.add_argument('-n', '--exp', dest="exp_name", default="noname",
                        help="Experiment name")
    parser.add_argument('-c', '--config', dest="exp_cfg", default="nocfg",
                        help="Experiment configuration")
    parser.add_argument('-s', '--scripts', dest="out_dir",
                        default="%s/rscfl_exp" % os.environ["HOME"],
                        help="Destination directory for experiment data. "
                             "This must exist on fg_load_vm and it must contain"
                             " all the data processing scripts")
    parser.add_argument('--meta', dest="meta", default="nometa",
                        help="Additional description/metadata for experiment")
    parser.add_argument('--fg_load_vm', dest="fg_load_vm",
                        default=fg_load_vm,
                        help="Machine driving fg load (ab), data pre-processing"
                        " and plotting")
    parser.add_argument('--bg_load_vm', dest="bg_load_vm",
                        default=bg_load_vm,
                        help="Machine driving bg load (stress), and controlling"
                        " contention (no of VMs, containers etc.)")
    parser.add_argument('--target_vm', dest="target_vm",
                        default=target_vm,
                        help="Machine running rscfl and lighttpd (or different"
                        " target process)")
    parser.add_argument('--proxy', dest="proxy",
                        default=None,
                        help="Set proxy for http requests")
    parser.add_argument('--manual', dest="manual_exp", action="store_true",
                        help="Manually run experiment. You will be guided"
                        " step-by-step in what needs to be done. This"
                        " overrides -c (--config)")
    args = parser.parse_args()

    fabric.api.output["stdout"] = False
    fabric.api.output["running"] = False


    proxies = {}
    if(args.proxy != None):
        import requesocks as requests
        proxies["http"] = args.proxy;
        proxies["https"] = args.proxy;
    else:
        import requests

    # load experiment config unless running manually:
    if not args.manual_exp:
        if args.exp_cfg == "nocfg":
            print("You must specify an experiment configuration file if not passing --manual")
        else:
            print("Loading experiment configuration...")
            cfg_file = open(args.exp_cfg)
            cfg_json = json.load(cfg_file)
            cfg_file.close()
            if args.exp_name == "noname":
                args.exp_name = cfg_json['exp-name']
            if args.meta == "nometa":
                args.meta = cfg_json['exp-descr']


    ## Here we go, preparing global experiment metadata
        msg="""
Checklist (please verify that the following are true):
  * iptables configured on {0};
  * rscfl is running on {0}, release build;
  * lighttpd is running on {0};
  * bash scripts you run have the #!/bin/bash directive

If one of those conditions is false, expect the script to stall and fail."""

    run_DAQ = False
    if cfg_json['exp-run-DAQ'] == "True":
        run_DAQ = True

    if run_DAQ == True:
        print(msg.format(args.target_vm))

    # Create experiment directory on fg_load_vm
    exp_dir = fabric.api.execute(create_experiment_dir,
                                   args.out_dir, args.exp_name, args.meta,
                                   hosts=args.fg_load_vm)

    # Infer basic experiment metadata (virt/no_virt, rscfl version, uname, etc)
    (out_path, result_path, data_path, exp_dir_name) = exp_dir[args.fg_load_vm]
    config_vars['exp_dir'] = out_path
    config_vars['exp_dir_name'] = exp_dir_name
    config_vars['script_dir'] = args.out_dir
    config_vars['data_dir'] = data_path
    config_vars['result_dir'] = result_path
    config_vars['target_vm'] = args.target_vm

    base_meta = {}
    if run_DAQ == True:
        base_meta = fabric.api.execute(get_target_meta, hosts=args.target_vm)
    else:
        base_meta[args.target_vm] = ", \"daq\": \"False\""
    script_rev = fabric.api.execute(get_script_rev, args.out_dir,
                                    hosts=args.fg_load_vm)

    fabric.api.execute(add_meta, out_path, script_rev[args.fg_load_vm],
                       hosts=args.fg_load_vm)
    fabric.api.execute(add_meta, out_path, base_meta[args.target_vm],
                       hosts=args.fg_load_vm)
    fabric.api.execute(copy_to_remote, args.exp_cfg, out_path + "/config.json",
                       hosts=args.fg_load_vm)

    if args.manual_exp == True:
        confirm = fabric.contrib.console.confirm(msg.format(args.target_vm))
        if not confirm:
            end_meta(out_path, args.fg_load_vm)
            return;

        # reset lighttpd accounting data
        requests.get("http://%s/rscfl/clear" % args.target_vm, proxies=proxies)

        # send mark for id 0 (required)
        payload = {'mark': 'exp_%s' % args.exp_name }
        requests.post("http://%s/mark" % args.target_vm, payload, proxies=proxies)

        # Guided Experiment -- stage 1
        # (running the experiment and the background load)
        bg_load_meta = get_experiment_load_meta(args.bg_load_vm,
                        "Start background load script on %s and then confirm (Y)"
                        % args.bg_load_vm)
        fabric.api.execute(add_meta,
                           out_path, ">bg_load=\n" + bg_load_meta,
                           hosts=args.fg_load_vm)
        fg_load_meta = get_experiment_load_meta(args.fg_load_vm,
                        "Confirm (Y) after starting the foreground load (ab) on %s"
                        % args.fg_load_vm)
        fabric.api.execute(add_meta,
                           out_path, ">fg_load=\n" + fg_load_meta,
                           hosts=args.fg_load_vm)
    else:
        if cfg_json['exp-run-DAQ'] == "True":
            # reset lighttpd accounting data
            requests.get("http://%s/rscfl/clear" % args.target_vm, proxies=proxies)

            # send mark for id 0 (required)
            payload = {'mark': 'exp_%s' % args.exp_name }
            requests.post("http://%s/mark" % args.target_vm, payload, proxies=proxies)


            # run background load
            bgld = cfg_json['bg-load']
            outStream = BgLoadScanIO(args, cfg_json)
            if(bgld['run'] == "True"):
                bg_load_cmd = config_process_vars(bgld['start'], cfg_json)
                bg_load_cmd_esc = bg_load_cmd.replace('"', '\\\\"')
                fabric.api.execute(add_meta, out_path,
                        ", \"bg_load_cmd\": \"%s\"" % bg_load_cmd_esc, hosts=args.fg_load_vm)
                fabric.api.output["stdout"] = True
                fabric.api.execute(run_bg_load, bg_load_cmd, outStream, hosts=args.bg_load_vm)
                fabric.api.output["stdout"] = False

            # run foreground load
            # <this is triggered by the stdout of the background load and executed
            #  by BgLoadScanIO>
            fgld = cfg_json['fg-load']
            if(bgld['run'] == "False" and fgld['run'] == "True"):
                outStream.write("$ESTART")
            outStream.close()

            # stop bg load (no reason to keep loading the vms)
            if(bgld['run'] == "True"):
                stop_bg_cmd = config_process_vars(cfg_json['stop-bg-load'], cfg_json)
                fabric.api.execute(run_cmd, stop_bg_cmd,
                                   "Stopping background load", hosts=args.bg_load_vm)

            # run processing
            if(fgld['run'] == "True"):
                process_cmd = config_process_vars(cfg_json['fg-process-raw']['script'], cfg_json)
                process_cmd = process_cmd + " " + config_vars['script_dir'] + "/"
                fabric.api.execute(run_cmd, process_cmd,
                                   "Parsing experiment data on %s" % args.fg_load_vm,
                                   hosts=args.fg_load_vm)
            # DAQ done

        local_vars = {}
        # train model
        tm = cfg_json['train-model']
        train_file = ""
        training_meta = ", \"training\": { \"run\": \"" + tm['run'] + "\""
        if(tm['run'] == "True"):
           bm_file = os.path.join(config_vars['script_dir'], tm['bare-metal-exp'],
                                  "data", tm['bm-sdat'])
           vm_file = config_process_vars(tm['virt-sdat'], cfg_json)
           out_tfile = config_process_vars(tm['out'], cfg_json)
           train_file = out_tfile
           local_vars['bm_file'] = bm_file
           local_vars['vm_file'] = vm_file
           local_vars['out_tfile'] = out_tfile
           local_vars['train_file'] = out_tfile
           training_meta = training_meta + ", \"bare-metal\": \"" + bm_file + "\""
           training_meta = training_meta + ", \"virt\": \"" + vm_file + "\""
           train_script = config_process_vars(tm['script'], cfg_json, local_vars)
           fabric.api.execute(run_cmd, train_script,
                    "Training gaussian process, into %s:%s" % (args.fg_load_vm, local_vars['out_tfile']),
                    hosts=args.fg_load_vm)
           for aux_out in tm['aux-out']:
               aux_out_s = config_process_vars(aux_out, cfg_json)
               fabric.api.execute(copy_file, aux_out_s,
                                  config_vars['result_dir'], True,
                                  hosts=args.fg_load_vm)
        elif(tm['run'] == "External"):
            train_fp = os.path.join(config_vars['script_dir'], tm['use-from'], "data", tm['name'])
            training_meta = training_meta + ", \"file\": \"" + train_fp + "\""
            local_vars['train_file'] = train_fp
        elif(tm['run'] == "False"):
            print("Skipping gaussian process training phase")
        training_meta = training_meta + " }"
        fabric.api.execute(add_meta, out_path, training_meta, hosts=args.fg_load_vm)

        data_fp = []
        out_fp = []

        #plot_scatter
        pscttr = cfg_json['plot-scatter']
        run_scatter = False;
        if pscttr['run'] == "True":
           run_scatter = True
           data_fp.append(config_process_vars(cfg_json['fg-process-raw']['out'][1], cfg_json))
           out_fp.append(os.path.join(result_path, config_process_vars(pscttr['out'], cfg_json)))
        elif pscttr['run'] == "External":
           run_scatter = True
           if type(pscttr['name']) in (list,):
               for idx, file_name in enumerate(pscttr['name']):
                   data_fp.insert(idx, os.path.join(config_vars['script_dir'], pscttr['use-from'], "data", file_name))
                   out_fp.insert(idx, os.path.join(result_path, config_process_vars(pscttr['out'][idx], cfg_json)))
           else:
               data_fp.append(os.path.join(config_vars['script_dir'], pscttr['use-from'], "data", pscttr['name']))
               out_fp.append(os.path.join(result_path, config_process_vars(pscttr['out'], cfg_json)))
        if run_scatter == True:
            for idx, data_file in enumerate(data_fp):
                local_vars['d_file_path'] = data_file
                local_vars['out_file'] = out_fp[idx]
                scttr_script = config_process_vars(pscttr['script'], cfg_json, local_vars)
                fabric.api.execute(run_cmd, scttr_script,
                        "Scatter plot latency vs sched-out [%d of %d]" % (idx + 1, len(data_fp)),
                        hosts=args.fg_load_vm)

        #plot-inducedlat-hist
        data_fp = []
        out_fp = []
        pilh = cfg_json['plot-inducedlat-hist']
        run_pilh = False;
        if pilh['run'] == "True":
           run_pilh = True
           data_fp.append(config_process_vars(cfg_json['fg-process-raw']['out'][1], cfg_json))
           out_fp.append(os.path.join(result_path, config_process_vars(pilh['out'], cfg_json)))
        elif pilh['run'] == "External":
           run_pilh = True
           if type(pilh['name']) in (list,):
               for idx, file_name in enumerate(pilh['name']):
                   data_fp.insert(idx, os.path.join(config_vars['script_dir'], pilh['use-from'], "data", file_name))
               for idx, out_name in enumerate(pilh['out']):
                   out_fp.insert(idx, os.path.join(result_path, config_process_vars(out_name, cfg_json)))
           else:
               data_fp.append(os.path.join(config_vars['script_dir'], pilh['use-from'], "data", pilh['name']))
               out_fp.append(os.path.join(result_path, config_process_vars(pilh['out'], cfg_json)))
        if run_pilh == True:
            if 'multiple-file-args' in pilh.keys() and pilh['multiple-file-args'] == "True":
                for idx, data_file in enumerate(data_fp):
                    local_vars['d_file_path'+str(idx)] = data_file
                local_vars['out_file'] = out_fp[0]
                pilh_script = config_process_vars(pilh['script'], cfg_json, local_vars)
                fabric.api.execute(run_cmd, pilh_script,
                        "Histogram of hypervisor-induced latency",
                        hosts=args.fg_load_vm)
            else:
                for idx, data_file in enumerate(data_fp):
                    local_vars['d_file_path'] = data_file
                    local_vars['out_file'] = out_fp[idx]
                    pilh_script = config_process_vars(pilh['script'], cfg_json, local_vars)
                    fabric.api.execute(run_cmd, pilh_script,
                            "Histograms of hypervisor-induced latency [%d of %d]" % (idx + 1, len(data_fp)),
                            hosts=args.fg_load_vm)

    end_meta(out_path, args.fg_load_vm)
    print("Copying results locally")
    fabric.api.execute(copy_from_remote, config_vars['result_dir'],
                       os.path.join(".", exp_dir_name),
                       hosts=args.fg_load_vm)
    fabric.api.execute(copy_from_remote,
                       os.path.join(config_vars['exp_dir'], "meta"),
                       os.path.join(".", exp_dir_name),
                       hosts=args.fg_load_vm)
    fabric.api.execute(copy_from_remote,
                       os.path.join(config_vars['exp_dir'], "config.json"),
                       os.path.join(".", exp_dir_name),
                       hosts=args.fg_load_vm)
    print("Teleporting unicorns from another dimension...[Experiment Done]")


if __name__ == "__main__":
    main()
