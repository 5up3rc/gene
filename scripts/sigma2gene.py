#!/usr/bin/env python

import os
import yaml
import json
import copy
import sys
import argparse
from itertools import combinations

VERSION = "1.0"
COPYRIGHT = "Sigma2Gene Copyright (C) 2018 RawSec SARL (@0xrawsec)"
LICENSE = "License GPLv3: This program comes with ABSOLUTELY NO WARRANTY.\nThis is free software, and you are welcome to redistribute it under certain conditions;"

SIGMA_EXTS = set([".yml"])
SYSMON_CHANNEL = "Microsoft-Windows-Sysmon/Operational"
SECURITY_CHANNEL = "Security"
GENE_TEMPLATE = {
  "Name": "",
  "Tags": [],
  "Meta": {
    "EventIDs": [],
    "Channels": [],
    "Computers": [],
    "Traces": [],
    "Criticality": 0,
    "Disable": False
  },
  "Matches": [],
  "Condition": ""
}

def log(message, file=sys.stderr):
    print(message, file=file)


# utility to recursively crawl directory
def crawl(dir, exts):
    if os.path.isdir(dir):
        for dirpath, dirnames, filenames in os.walk(dir):
            for fn in filenames:
                ext = ".{0}".format(fn.rsplit(os.extsep)[-1])
                if ext in exts:
                    yield os.path.join(dirpath, fn)
    elif os.path.isfile(dir):
        yield dir

def yml_parser(ymlfile):
    with open(ymlfile) as fd:
        for rule in yaml.safe_load_all(fd.read()):
            yield rule

def rec_get(d, *path):
    if len(path) > 1:
        if path[0] in d:
            return rec_get(d[path[0]], *path[1:])
    if len(path) == 1:
        if path[0] in d:
            return d[path[0]]
    return None

def leaf_with_key(d, key):
    if key in d:
        yield d
    for k in d:
        if isinstance(d[k], dict):
            for dd in leaf_with_key(d[k], key):
                yield dd

def sigma_sel2field_match(var, field, rule):
    rule = rule or ""
    if isinstance(rule, int):
        return "{var}: {field} = '{rule}'".format(var=var, field=field, rule=rule)

    rule = rule.replace("\\", "\\\\")
    rule = rule.replace(".", "\\.")
    rule = rule.replace("*", ".*")
    return "{var}: {field} ~= '(?i:{rule})'".format(var=var, field=field, rule=rule)

def critconv(sigma_crit):
    if sigma_crit == "critical":
        return 10
    elif sigma_crit == "high":
        return 8
    elif sigma_crit == "medium":
        return 6
    elif sigma_crit == "low":
        return 3
    return 5

def merge_dict(d1, d2):
    merged = copy.deepcopy(d1)
    for k in d2:
        if k not in merged:
            merged[k] = d2[k]
        else:
            if type(merged[k]) == type(d2[k]):
                if isinstance(merged[k], dict):
                    for e in d2[k]:
                        merged[k][e] = d2[k][e]
                elif isinstance(merged[k], list):
                    merged[k] += d2[k]
    return merged


def sigma2gene(sigma_rule, skeleton=None, path=None):
    '''
        converts a sigma rule to a gene rule
        @sigma_rule: sigma rule to convert
        @skeleton: global rule the sigma rule inherits from
        @path: path of the rule
    '''
    gene = copy.deepcopy(GENE_TEMPLATE)
    skeleton = skeleton or sigma_rule
    title = skeleton["title"]
    if "title" in skeleton:
        gene["Name"] = skeleton["title"].replace(" ","")
    gene["Meta"]["Author"] = rec_get(skeleton, "author")
    gene["Meta"]["Comments"] = rec_get(skeleton, "description")
    gene["Meta"]["References"] = rec_get(skeleton, "references")
    gene["Meta"]["Disclaimer"] = "This rule has been auto-generated by a script. It has not been optimized for Gene and may cause a slow down of the engine or unexpected results."
    if path is not None:
        gene["Meta"]["SigmaSource"] = os.path.basename(path)
    gene["Tags"] += ["Sigma", "Auto-generated"]

    # translating service
    service = rec_get(sigma_rule, "logsource", "service")
    if service is not None:
        if service.lower() == "sysmon":
            gene["Meta"]["Channels"].append(SYSMON_CHANNEL)
        elif service.lower() == "security":
            gene["Meta"]["Channels"].append(SECURITY_CHANNEL)
    
    detection_elements = {}
    if "detection" in sigma_rule and "detection" in skeleton:
        sigma_rule["detection"] = merge_dict(sigma_rule["detection"], skeleton["detection"])
    elif "detection" in skeleton:
        sigma_rule["detection"] = skeleton["detection"]

    # handle the variables
    i = 0
    tmp_cond = {}
    for k in sigma_rule["detection"]:
        if sigma_rule["detection"][k] is None:
            continue
        if k in set(["condition", "timeframe"]):
            continue
        elif "EventID" in sigma_rule["detection"][k] and isinstance(sigma_rule["detection"][k], dict):
            # manages the other fields
            selection = sigma_rule["detection"][k]
            for field in selection:
                var = "$v{0}".format(i)
                if field == "EventID":
                    eventids = selection["EventID"]
                    if eventids is not None:
                        eventids = [eventids] if not isinstance(eventids, list) else eventids
                        for eventid in eventids:
                            if int(eventid) not in gene["Meta"]["EventIDs"]:
                                gene["Meta"]["EventIDs"].append(int(eventid))
                    # We do not need to continue here
                    continue
                if isinstance(selection[field], list):
                    matches = selection[field]
                    gene["Matches"].append(sigma_sel2field_match(var, field, "|".join(["({0})".format(m) for m in matches])))
                else:
                    gene["Matches"].append(sigma_sel2field_match(var, field, selection[field]))
                if k not in tmp_cond:
                    tmp_cond[k] = []
                tmp_cond[k].append(var)
                i += 1
        else:
            log("[-] Rule skipped \"{0}->{1}\": not compatible with Gene specs".format(path, title))
            return
    # converts the lists of variables into strings
    tmp_cond = {k:" and ".join(tmp_cond[k]) for k in tmp_cond}

    # handle criticality
    level = rec_get(sigma_rule, "level")
    level = level or rec_get(skeleton, "level")
    # the sigma signature level is converted to an integer as Gene expects
    gene["Meta"]["Criticality"] = critconv(level)

    # handle the condition
    condition = sigma_rule["detection"]["condition"]
    if not isinstance(condition, str):
        log("[-] Rule skipped \"{0}->{1}\": cannot translate condition ({2})".format(path, title, sigma_rule["detection"]["condition"]))
        return    
    if condition in tmp_cond:
        gene["Condition"] = tmp_cond[condition]
    elif condition.endswith("of them"):
        num = condition.split(" ")[0].strip()
        if num == "all":
            gene["Condition"] = " and ".join(["{0}".format(c[0]) for c in tmp_cond.values])
        else:
            comb = combinations(tmp_cond.values(), int(num))
            gene["Condition"] = " or ".join(["({0})".format(c[0]) for c in comb])
    else:
        log("[-] Rule skipped \"{0}->{1}\": don't handle yet condition ({2})".format(path, title, condition))
        return

    return gene


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Sigma to Gene converter")
    parser.add_argument("-v", "--version", action="store_true", help="Show version of the script and exits")
    parser.add_argument("-r", "--rules", nargs="*", help="Path to process, can be a file or a directory")

    args = parser.parse_args()

    if args.version:
        print("Version: {0}".format(VERSION))
        print("Copyright: {0}".format(COPYRIGHT))
        print(LICENSE)
        sys.exit(0)

    rule_cnt = 0
    rule_names = set([])
    for path in args.rules:
        for f in crawl(path, SIGMA_EXTS):
            skeleton = None
            for sigma_rule in yml_parser(f):
                if "action" in sigma_rule:
                    if sigma_rule["action"] == "global":
                        skeleton = sigma_rule
                        continue
                gene = sigma2gene(sigma_rule, skeleton, f)
                if gene is not None:
                    if gene["Name"] in rule_names:
                        i = 2
                        while "{0}#{1}".format(gene["Name"],i) in rule_names:
                            i += 1
                        gene["Name"] = "{0}#{1}".format(gene["Name"],i)
                    print(json.dumps(gene))
                    rule_names.add(gene["Name"])
                    rule_cnt += 1
    log("[+] Rules converted with success: {0}".format(rule_cnt))
