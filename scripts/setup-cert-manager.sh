#!/usr/bin/env bash
# Optional certificate-controller/Issuer setup. Never runs during normal install.
set -euo pipefail
set +x
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
for argument in "$@"; do
  if [[ "$argument" == --ssh || "$argument" == --ssh=* ]]; then
    exec python3 "$root/scripts/install-remote.py" --setup-cert-manager "$@"
  fi
done
exec python3 - "$root" "$@" <<'PY'
import argparse,hashlib,importlib.util,json,os,pathlib,pwd,re,shutil,subprocess,sys,tempfile
root=pathlib.Path(sys.argv[1])
p=argparse.ArgumentParser(description='Explicit optional cert-manager and namespaced Issuer setup; no application deployment or device trust changes.')
p.add_argument('--local-k3s',action='store_true',help='Use this K3s host (sudo required), optionally with --kubeconfig/--context')
p.add_argument('--context',help='Explicit Kubernetes context; local K3s defaults to default')
p.add_argument('--kubeconfig')
p.add_argument('--namespace',required=True)
p.add_argument('--issuer',required=True)
p.add_argument('--email',help='ACME account email for public HTTP01 issuance')
p.add_argument('--ingress-class',default='traefik')
p.add_argument('--issuer-file',type=pathlib.Path,help='Private mode0600 JSON document containing one reviewed namespaced Issuer, such as DNS01 provider configuration')
p.add_argument('--install-controller',action='store_true',help='Explicitly install the pinned global cert-manager controller and CRDs if absent')
p.add_argument('--dry-run',action='store_true',help='Print the HTTP01 plan without contacting any cluster')
a=p.parse_args(sys.argv[2:])
if not a.local_k3s and not a.context:p.error('Select --local-k3s or an explicit --context')
def name(value):
    return bool(value) and len(value)<=253 and all(len(part)<=63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?',part) for part in value.split('.'))
if not all(name(v) for v in [a.namespace,a.issuer,a.ingress_class]) or len(a.namespace)>63:
    p.error('Namespace, issuer and ingress class must be valid lowercase DNS names')
if bool(a.email)==bool(a.issuer_file):
    p.error('Choose --email for HTTP01 or --issuer-file for a reviewed provider/CA configuration')
if a.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',a.email):p.error('Provide a valid ACME account email')
if a.issuer_file:
    try:
        if not a.issuer_file.is_file() or a.issuer_file.is_symlink() or a.issuer_file.stat().st_mode & 0o077:
            p.error('--issuer-file must be a private mode0600 regular JSON file')
        supplied=json.loads(a.issuer_file.read_text())
    except (OSError,ValueError):
        p.error('--issuer-file must contain a single JSON object; JSON is supported without installing a YAML parser')
    if not isinstance(supplied,dict) or not isinstance(supplied.get('metadata'),dict) or not isinstance(supplied.get('spec'),dict) or not supplied['spec'] or 'items' in supplied or supplied.get('apiVersion')!='cert-manager.io/v1' or supplied.get('kind')!='Issuer' or supplied['metadata'].get('name')!=a.issuer or supplied['metadata'].get('namespace')!=a.namespace:
        p.error('Issuer file must contain exactly one namespaced cert-manager Issuer matching --issuer and --namespace; other resources/lists are refused before any cluster action')
    acme=supplied['spec'].get('acme',{})
    if not isinstance(acme,dict) or not isinstance(acme.get('solvers',[]),list):p.error('Issuer ACME configuration must be an object with a solver list')
    for solver in acme.get('solvers',[]):
        if isinstance(solver,dict) and 'dns01' in solver and not solver['dns01']:
            p.error('Fill the DNS01 scaffold with your provider configuration and existing credential Secret references before setup')
lock=json.loads((root/'scripts/cert-manager.lock.json').read_text())
def command(argv,data=None):
    result=subprocess.run([str(v) for v in argv],input=data,text=True,capture_output=True,timeout=660)
    if result.returncode:raise RuntimeError('A setup command failed. Check target permissions, controller health or issuer configuration; potentially sensitive output was withheld.')
    return result.stdout
account='wf-acme-'+hashlib.sha256(a.issuer.encode()).hexdigest()[:16]
issuer={'apiVersion':'cert-manager.io/v1','kind':'Issuer','metadata':{'name':a.issuer,'namespace':a.namespace},'spec':{'acme':{'email':a.email,'server':'https://acme-v02.api.letsencrypt.org/directory','privateKeySecretRef':{'name':account},'solvers':[{'http01':{'ingress':{'ingressClassName':a.ingress_class}}}]}}}
if a.dry_run:
    if a.issuer_file:p.error('--dry-run prints only the generated HTTP01 plan; inspect your supplied issuer file directly')
    print(json.dumps({'target':'local K3s' if a.local_k3s else a.context,'globalControllerInstall':a.install_controller,'controllerVersion':lock['version'],'issuer':issuer,'prerequisites':'HTTP01 needs an owned public domain resolving to the ingress and public port 80 reachability. Private-LAN-only services need a configured DNS01 issuer instead.'},indent=2))
    sys.exit(0)
try:
    if a.local_k3s:
        config=pathlib.Path(a.kubeconfig or '/etc/rancher/k3s/k3s.yaml').expanduser().absolute()
        context=a.context or 'default'
        if os.geteuid()!=0 or not config.is_file():raise RuntimeError('Use sudo on the configured K3s server with its existing kubeconfig')
        binary=shutil.which('k3s')
        if not binary:raise RuntimeError('k3s executable was not found')
        kube=[binary,'kubectl','--kubeconfig',str(config),'--context',context]
        helm_target=['--kubeconfig',str(config),'--kube-context',context]
    else:
        binary=shutil.which('kubectl')
        if not binary:raise RuntimeError('kubectl is required for an explicit context')
        kube=[binary]+(['--kubeconfig',a.kubeconfig] if a.kubeconfig else [])+['--context',a.context]
        helm_target=(['--kubeconfig',a.kubeconfig] if a.kubeconfig else [])+['--kube-context',a.context]
    def get(kind,value,namespace=None):
        output=command(kube+(['-n',namespace] if namespace else [])+['get',kind,value,'--ignore-not-found','-o','json'])
        return json.loads(output) if output.strip() else None
    version=json.loads(command(kube+['version','-o','json']))['serverVersion']['gitVersion']
    print('Target: '+('local K3s' if a.local_k3s else a.context)+'; Kubernetes '+version+'; application namespace '+a.namespace)
    crd=get('customresourcedefinition','certificates.cert-manager.io')
    if a.install_controller:
        if crd:raise RuntimeError('cert-manager CRDs already exist; no existing operator was adopted or changed. Remove --install-controller and use the current operator.')
        parsed=re.match(r'v?(\d+)\.(\d+)',version)
        if not parsed or int(parsed[1])!=1 or not lock['kubernetesMinMinor']<=int(parsed[2])<=lock['kubernetesMaxMinor']:
            raise RuntimeError('This pinned cert-manager bootstrap supports Kubernetes 1.33–1.36. Install an operator compatible with this cluster separately, or select controller/private-CA mode.')
        spec=importlib.util.spec_from_file_location('wf_installer_tools',root/'scripts/installer.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        user=pwd.getpwnam(os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name)
        directory=module.secure_directory(pathlib.Path(user.pw_dir)/'.local/share/wealthfolio-certificate-setup/tools',user.pw_uid,user.pw_gid)
        helm=shutil.which('helm') or module.locked_tool('helm',directory)
        try:
            status_flags=module.helm_list_status_flags(helm)
        except module.InstallError as error:
            raise RuntimeError(str(error)) from None
        releases=json.loads(command([helm,'list',*helm_target,'-n','cert-manager',*status_flags,'-o','json']))
        if releases:raise RuntimeError('An existing cert-manager namespace release was found; no release was adopted or upgraded')
        print('Explicit --install-controller: installing cert-manager '+lock['version']+' and its cluster-scoped CRDs/RBAC. Other installed operators are not modified.')
        with tempfile.TemporaryDirectory(prefix='wf-cert-manager-') as temporary:
            command([helm,'pull',lock['repository']+'@'+lock['manifestDigest'],'--destination',temporary])
            archives=list(pathlib.Path(temporary).glob('*.tgz'))
            if len(archives)!=1 or hashlib.sha256(archives[0].read_bytes()).hexdigest()!=lock['archiveSha256']:raise RuntimeError('cert-manager chart checksum mismatch; nothing was installed')
            command([helm,'install','cert-manager',archives[0],*helm_target,'--namespace','cert-manager','--create-namespace','--set','crds.enabled=true','--wait','--timeout','5m'])
    elif not crd:
        raise RuntimeError('cert-manager is absent. Rerun this helper with --install-controller only after reviewing its global scope, or choose the controller/private-CA HTTPS path.')
    if get('issuer.cert-manager.io',a.issuer,a.namespace):raise RuntimeError('The selected Issuer already exists and was not changed. Inspect it and select it in the installer instead.')
    if a.issuer_file:
        issuer=supplied
    if not get('namespace',a.namespace):command(kube+['create','-f','-'],json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':a.namespace}}))
    if a.email:print('HTTP01 prerequisite: an owned public domain must resolve to this ingress and allow public port 80 validation. This helper does not configure DNS/firewalls.')
    command(kube+['-n',a.namespace,'create','-f','-'],json.dumps(issuer))
    command(kube+['-n',a.namespace,'wait','--for=condition=Ready','issuer.cert-manager.io/'+a.issuer,'--timeout=300s'])
    print('Issuer is Ready. Select --tls-mode cert-manager --issuer '+a.issuer+' --issuer-kind Issuer in the application installer. Certificate issuance/renewal remains the operator and issuer responsibility.')
except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
    print('Certificate setup stopped: '+(str(error) if isinstance(error,RuntimeError) else 'A target response or local dependency could not be used safely.'),file=sys.stderr)
    sys.exit(1)
PY
