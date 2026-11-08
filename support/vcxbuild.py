#!/usr/bin/env python3
"""Build one configuration of a .vcxproj with msvc-wine (cl/link/lib/rc), no MSBuild.

MSBuild itself does not run under Wine without .NET, so this reads the project
the way MSBuild would for the settings these projects use and drives the MSVC
tools directly. The upstream .vcxproj stays the single source of truth for
sources, defines, include dirs, .def files and libraries.

    vcxbuild.py <project.vcxproj> [--sln X.sln] [--config Release] [--platform Win32]
                [--out DIR] [--toolset v143] [-j N]

--config is the solution configuration when --sln is given: each project is
built in the configuration the solution maps it to (ProjectConfigurationPlatforms),
and $(SolutionDir) is the directory of the .sln.

Prints the output file on success. Unknown project settings are reported as
warnings so a new upstream setting does not go unnoticed. Run inside the
msvc-wine image (tools in /opt/msvc/bin/<arch>).
"""
import argparse
import concurrent.futures
import hashlib
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
TOOLS = os.environ.get("MSVC_BIN", "/opt/msvc/bin/x86")
DEFAULT_LIBS = ("kernel32.lib user32.lib gdi32.lib winspool.lib comdlg32.lib advapi32.lib "
                "shell32.lib ole32.lib oleaut32.lib uuid.lib odbc32.lib odbccp32.lib").split()

warned = set()


def warn(msg):
    if msg not in warned:
        warned.add(msg)
        print("vcxbuild: warning: " + msg, file=sys.stderr)


def tag(e):
    return e.tag.split("}", 1)[1]


def winpath(p):
    """Relative path in Windows form; never starts with '/', so cl cannot read it as an option."""
    p = os.path.normpath(p).replace("/", "\\")
    if p.startswith("\\"):
        sys.exit("vcxbuild: absolute path %s: run from a directory above it" % p)
    return p


class Project:
    def __init__(self, path, config, platform, solution_dir, toolset):
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        self.root = ET.parse(path).getroot()
        self.macros = {
            "Configuration": config,
            "Platform": platform,
            "PlatformToolset": toolset,
            "ProjectDir": os.path.relpath(self.dir) + os.sep,
            "SolutionDir": os.path.relpath(solution_dir or self.dir) + os.sep,
            "ProjectName": os.path.splitext(os.path.basename(path))[0],
        }
        self.props = {}
        for pg in self.root.findall("m:PropertyGroup", NS):
            if not self.cond(pg.get("Condition")):
                continue
            for x in pg:
                if self.cond(x.get("Condition")):
                    self.props[tag(x)] = (x.text or "").strip()
        self.macros["TargetName"] = self.props.get("TargetName") or self.macros["ProjectName"]
        self.defs = {}  # section -> {key: value}
        for idg in self.root.findall("m:ItemDefinitionGroup", NS):
            if not self.cond(idg.get("Condition")):
                continue
            for sec in idg:
                d = self.defs.setdefault(tag(sec), {})
                for x in sec:
                    if self.cond(x.get("Condition")):
                        d[tag(x)] = (x.text or "").strip()

    def expand(self, s):
        def rep(m):
            k = m.group(1)
            if k in self.macros:
                return self.macros[k]
            warn("unknown macro $(%s), expanded to nothing" % k)
            return ""
        return re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", rep, s or "")

    def cond(self, c):
        """MSBuild conditions as used in project files: 'a'=='b', !=, and/or."""
        if not c:
            return True
        c = self.expand(c)
        def one(t):
            m = re.fullmatch(r"\s*'([^']*)'\s*(==|!=)\s*'([^']*)'\s*", t)
            if not m:
                warn("cannot evaluate condition %r, taken as false" % t)
                return False
            eq = m.group(1).lower() == m.group(3).lower()
            return eq if m.group(2) == "==" else not eq
        return any(all(one(a) for a in re.split(r"(?i)\s+and\s+", o)) for o in re.split(r"(?i)\s+or\s+", c))

    def items(self, kind):
        out = []
        for ig in self.root.findall("m:ItemGroup", NS):
            if not self.cond(ig.get("Condition")):
                continue
            for it in ig:
                if tag(it) != kind or not self.cond(it.get("Condition")):
                    continue
                meta = {}
                for x in it:
                    if self.cond(x.get("Condition")):
                        meta[tag(x)] = (x.text or "").strip()
                out.append((self.expand(it.get("Include")), meta))
        return out

    def src(self, include):
        return os.path.relpath(os.path.join(self.dir, include.replace("\\", "/")))


def split_list(v):
    return [x for x in (s.strip() for s in v.split(";")) if x and not x.startswith("%(")]


def cl_flags(p, s):
    """ClCompile settings -> cl options (project defaults first, like Microsoft.Cpp.props)."""
    f = ["/nologo", "/c", "/EHsc", "/GS", "/Gd", "/Zc:wchar_t", "/Zc:forScope", "/Zc:inline", "/fp:precise", "/Z7"]
    cs = p.props.get("CharacterSet", "")
    if cs == "Unicode":
        f += ["/DUNICODE", "/D_UNICODE"]
    elif cs == "MultiByte":
        f += ["/D_MBCS"]
    if p.props.get("ConfigurationType") == "DynamicLibrary":
        f += ["/D_WINDLL"]
    if p.props.get("WholeProgramOptimization") == "true" and s.get("WholeProgramOptimization", "true") == "true":
        f += ["/GL"]
    for k, v in s.items():
        v = p.expand(v)
        if k == "AdditionalIncludeDirectories":
            for d in split_list(v):
                d = d.replace("\\", "/")
                if os.path.isdir(d):
                    f.append("/I" + winpath(d))
        elif k == "PreprocessorDefinitions":
            f += ["/D" + d for d in split_list(v)]
        elif k == "UndefinePreprocessorDefinitions":
            f += ["/U" + d for d in split_list(v)]
        elif k == "WarningLevel":
            f.append({"TurnOffAllWarnings": "/W0", "Level1": "/W1", "Level2": "/W2", "Level3": "/W3", "Level4": "/W4", "EnableAllWarnings": "/Wall"}[v])
        elif k == "TreatWarningAsError":
            f.append("/WX" if v == "true" else "/WX-")
        elif k == "DisableSpecificWarnings":
            f += ["/wd" + w for w in split_list(v)]
        elif k == "Optimization":
            f.append({"Disabled": "/Od", "MinSpace": "/O1", "MaxSpeed": "/O2", "Full": "/Ox"}[v])
        elif k == "InlineFunctionExpansion":
            f.append({"Disabled": "/Ob0", "OnlyExplicitInline": "/Ob1", "AnySuitable": "/Ob2", "Default": ""}[v])
        elif k == "FavorSizeOrSpeed":
            f.append({"Speed": "/Ot", "Size": "/Os", "Neither": ""}[v])
        elif k == "OmitFramePointers":
            f.append("/Oy" if v == "true" else "/Oy-")
        elif k == "IntrinsicFunctions":
            f.append("/Oi" if v == "true" else "")
        elif k == "StringPooling":
            f.append("/GF" if v == "true" else "")
        elif k == "FunctionLevelLinking":
            f.append("/Gy" if v == "true" else "/Gy-")
        elif k == "BufferSecurityCheck":
            f.append("/GS" if v == "true" else "/GS-")
        elif k == "SDLCheck":
            f.append("/sdl" if v == "true" else "")
        elif k == "FloatingPointModel":
            f.append({"Precise": "/fp:precise", "Strict": "/fp:strict", "Fast": "/fp:fast"}[v])
        elif k == "EnableEnhancedInstructionSet":
            f.append({"StreamingSIMDExtensions": "/arch:SSE", "StreamingSIMDExtensions2": "/arch:SSE2",
                      "AdvancedVectorExtensions": "/arch:AVX", "AdvancedVectorExtensions2": "/arch:AVX2",
                      "NoExtensions": "/arch:IA32", "NotSet": ""}[v])
        elif k == "RuntimeLibrary":
            f.append({"MultiThreaded": "/MT", "MultiThreadedDebug": "/MTd", "MultiThreadedDLL": "/MD", "MultiThreadedDebugDLL": "/MDd"}[v])
        elif k == "ExceptionHandling":
            f.remove("/EHsc")
            f.append({"Sync": "/EHsc", "Async": "/EHa", "SyncCThrow": "/EHs", "false": ""}[v])
        elif k == "RuntimeTypeInfo":
            f.append("/GR" if v == "true" else "/GR-")
        elif k == "LanguageStandard":
            f.append({"stdcpp14": "/std:c++14", "stdcpp17": "/std:c++17", "stdcpp20": "/std:c++20", "stdcpplatest": "/std:c++latest", "Default": ""}[v])
        elif k == "ConformanceMode":
            f.append("/permissive-" if v == "true" else "")
        elif k == "ForcedIncludeFiles":
            f += ["/FI" + x for x in split_list(v)]
        elif k == "CallingConvention":
            f.append({"Cdecl": "/Gd", "StdCall": "/Gz", "FastCall": "/Gr", "VectorCall": "/Gv"}[v])
        elif k == "AdditionalOptions":
            f += [o for o in v.split() if not o.startswith("%(")]
        elif k in ("PrecompiledHeader", "PrecompiledHeaderFile", "PrecompiledHeaderOutputFile", "ExcludedFromBuild",
                   "DebugInformationFormat", "WholeProgramOptimization", "MinimalRebuild", "BasicRuntimeChecks",
                   "MultiProcessorCompilation", "ProgramDataBaseFileName", "ObjectFileName", "AssemblerListingLocation",
                   "BrowseInformation", "CompileAs"):
            pass  # handled per file, or not relevant for a release build
        else:
            warn("ClCompile/%s=%s not supported, ignored" % (k, v))
    return [x for x in f if x]


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    out = "\n".join(l for l in r.stdout.splitlines() if l.strip() and not re.match(r"^\S+\.(c|cpp|cc|cxx)$", l.strip()))
    return r.returncode, out


def sln_config(a, guid):
    """Project configuration the solution maps a.config|a.platform to."""
    if not a.sln or not guid:
        return a.config
    key = "%s.%s|%s.ActiveCfg" % (guid.upper(), a.config, a.platform)
    for line in open(a.sln, encoding="utf-8-sig", errors="replace"):
        k, _, v = line.strip().partition(" = ")
        if k.upper() == key.upper():
            return v.split("|")[0]
    warn("%s has no mapping for %s in %s, using %s" % (guid, a.config, a.sln, a.config))
    return a.config


def project_guid(path):
    g = ET.parse(path).getroot().find("m:PropertyGroup/m:ProjectGuid", NS)
    return g.text.strip() if g is not None and g.text else None


def build(path, a, depth=0):
    config = sln_config(a, project_guid(path))
    p = Project(path, config, a.platform, os.path.dirname(a.sln) if a.sln else None, a.toolset)
    ctype = p.props.get("ConfigurationType", "Application")
    name = p.macros["TargetName"]
    obj = os.path.join(a.out, "obj", p.macros["ProjectName"] + "-" + config.replace(" ", "_"))
    os.makedirs(obj, exist_ok=True)
    print("== %s%s (%s|%s, %s)" % ("  " * depth, name, config, a.platform, ctype), flush=True)

    # Referenced projects that are linked in (static libraries) are built first.
    extra_libs = []
    for inc, meta in p.items("ProjectReference"):
        if meta.get("LinkLibraryDependencies", "true") != "false":
            extra_libs.append(build(p.src(inc), a, depth + 1))

    base = p.defs.get("ClCompile", {})
    pch_obj = None
    jobs = []
    for inc, meta in p.items("ClCompile"):
        if meta.get("ExcludedFromBuild") == "true":
            continue
        s = dict(base)
        s.update(meta)
        src = p.src(inc)
        o = os.path.join(obj, "%s_%s.obj" % (os.path.splitext(os.path.basename(src))[0],
                                            hashlib.sha1(src.encode()).hexdigest()[:8]))
        flags = cl_flags(p, s)
        if src.lower().endswith(".c") and s.get("CompileAs") != "CompileAsCpp":
            flags.append("/TC")
        pch = s.get("PrecompiledHeader", "NotUsing")
        pchfile = s.get("PrecompiledHeaderFile", "stdafx.h")
        fp = "/Fp" + winpath(os.path.join(obj, name + ".pch"))
        if pch == "Create":
            flags += ["/Yc" + pchfile, fp]
        elif pch == "Use":
            flags += ["/Yu" + pchfile, fp]
        cmd = [os.path.join(TOOLS, "cl")] + flags + ["/Fo" + winpath(o), winpath(src)]
        if pch == "Create":
            pch_obj = (cmd, o, src)
        else:
            jobs.append((cmd, o, src))

    objs = []
    failed = False
    if not pch_obj and jobs:
        # Compile one file alone first: the first Wine start may set up its prefix,
        # which must not happen in several processes at once.
        pch_obj = jobs.pop(0)
    if pch_obj:
        rc, out = run(pch_obj[0])
        if out:
            print(out)
        if rc:
            sys.exit("vcxbuild: %s failed" % pch_obj[2])
        objs.append(pch_obj[1])
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
        for (cmd, o, src), (rc, out) in zip(jobs, ex.map(lambda j: run(j[0]), jobs)):
            if out:
                print(out)
            if rc:
                print("vcxbuild: FAILED %s" % src, file=sys.stderr)
                failed = True
            objs.append(o)
    if failed:
        sys.exit("vcxbuild: compile errors in %s" % name)

    for inc, meta in p.items("ResourceCompile"):
        if meta.get("ExcludedFromBuild") == "true":
            continue
        src = p.src(inc)
        res = os.path.join(obj, os.path.splitext(os.path.basename(src))[0] + ".res")
        rcd = p.defs.get("ResourceCompile", {})
        cmd = [os.path.join(TOOLS, "rc"), "/nologo"] + ["/d" + d for d in split_list(p.expand(rcd.get("PreprocessorDefinitions", "")))]
        cmd += ["/i" + winpath(os.path.dirname(src) or "."), "/fo" + winpath(res), winpath(src)]
        rc, out = run(cmd)
        if out:
            print(out)
        if rc:
            sys.exit("vcxbuild: resource %s failed" % src)
        objs.append(res)

    ltcg = p.props.get("WholeProgramOptimization") == "true"
    if ctype == "StaticLibrary":
        target = os.path.join(a.out, name + ".lib")
        cmd = [os.path.join(TOOLS, "lib"), "/nologo"] + (["/LTCG"] if ltcg else []) + ["/OUT:" + winpath(target)]
        rc, out = run(cmd + [winpath(o) for o in objs])
        if out:
            print(out)
        if rc:
            sys.exit("vcxbuild: lib %s failed" % name)
        return target

    ext = {"DynamicLibrary": ".dll", "Application": ".exe"}[ctype]
    target = os.path.join(a.out, name + ext)
    lnk = p.defs.get("Link", {})
    f = ["/nologo", "/MACHINE:" + ("X86" if a.platform == "Win32" else a.platform.upper()),
         "/DYNAMICBASE", "/NXCOMPAT", "/MANIFEST:NO", "/OUT:" + winpath(target)]
    if ctype == "DynamicLibrary":
        f += ["/DLL", "/IMPLIB:" + winpath(os.path.join(obj, name + ".lib"))]
    if ltcg:
        f.append("/LTCG")
    libs = list(DEFAULT_LIBS)
    for k, v in lnk.items():
        v = p.expand(v)
        if k == "AdditionalDependencies":
            libs = [winpath(x.replace("\\", "/")) if ("/" in x or "\\" in x) else x for x in split_list(v)] + libs
        elif k == "AdditionalLibraryDirectories":
            f += ["/LIBPATH:" + winpath(d.replace("\\", "/")) for d in split_list(v) if os.path.isdir(d.replace("\\", "/"))]
        elif k == "ModuleDefinitionFile":
            if v:
                f.append("/DEF:" + winpath(p.src(v)))
        elif k == "GenerateDebugInformation":
            if v == "true":
                f += ["/DEBUG", "/PDB:" + winpath(os.path.join(a.out, name + ".pdb"))]
        elif k == "EnableCOMDATFolding":
            f.append("/OPT:ICF" if v == "true" else "/OPT:NOICF")
        elif k == "OptimizeReferences":
            f.append("/OPT:REF" if v == "true" else "/OPT:NOREF")
        elif k == "ImageHasSafeExceptionHandlers":
            f.append("/SAFESEH" if v == "true" else "/SAFESEH:NO")
        elif k == "SubSystem":
            f.append("/SUBSYSTEM:" + v.upper())
        elif k == "IgnoreSpecificDefaultLibraries":
            f += ["/NODEFAULTLIB:" + x for x in split_list(v)]
        elif k == "IgnoreAllDefaultLibraries":
            f.append("/NODEFAULTLIB" if v == "true" else "")
        elif k == "AdditionalOptions":
            f += [o for o in v.split() if not o.startswith("%(")]
        elif k in ("MinimumRequiredVersion", "OutputFile", "LinkIncremental", "TargetMachine"):
            pass
        else:
            warn("Link/%s=%s not supported, ignored" % (k, v))
    cmd = [os.path.join(TOOLS, "link")] + [x for x in f if x] + [winpath(o) for o in objs] + [winpath(x) for x in extra_libs] + libs
    rc, out = run(cmd)
    if out:
        print(out)
    if rc:
        sys.exit("vcxbuild: link %s failed" % name)
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--config", default="Release")
    ap.add_argument("--platform", default="Win32")
    ap.add_argument("--sln")
    ap.add_argument("--out", default="build-win")
    ap.add_argument("--toolset", default="v143")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()
    a.out = os.path.relpath(os.path.join(a.out, a.config.replace(" ", "_")))
    print(build(a.project, a))


if __name__ == "__main__":
    main()
