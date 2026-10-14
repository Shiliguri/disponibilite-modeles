#!/usr/bin/env python3
"""Mesure la disponibilité réelle des RR1h dans l'API ciblée Météo-France.
Une invocation = un cycle de sondage. GitHub Actions conserve data/ dans git.
python surveiller_modeles.py [--config config.json] [--report-only]
Clé : secret/env METEOFRANCE_API_KEY (jamais enregistré dans les résultats).
Toutes les dates internes sont UTC. Le rapport affiche aussi Europe/Paris.
"""
import argparse
import csv
import html
import io
import json
import math
import os
import statistics
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC=timezone.utc
RR_PREFIX='TOTAL_WATER_PRECIPITATION__GROUND_OR_WATER_SURFACE___'
BASE=[('service','WCS'),('version','2.0.1')]


def stamp(d):
    return d.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00','Z')


def instant(s):
    return datetime.fromisoformat(s.replace('Z','+00:00'))


def local(tag):
    return tag.rsplit('}',1)[-1]


def catalogs(content):
    root=ET.fromstring(content)
    if local(root.tag)!='Capabilities':
        raise ValueError('Réponse différente d’un catalogue WCS Capabilities.')
    result={}
    for e in root.iter():
        cid=(e.text or '').strip()
        if local(e.tag)=='CoverageId' and cid.startswith(RR_PREFIX) and cid.endswith('_PT1H'):
            text=cid[len(RR_PREFIX):-5]
            try:run=datetime.strptime(text,'%Y-%m-%dT%H.%M.%SZ').replace(tzinfo=UTC)
            except ValueError:continue
            result[stamp(run)]=cid
    return result


def exception_is_absent(content):
    """Seules les erreurs métier explicites sont des constats d'absence."""
    try:root=ET.fromstring(content)
    except ET.ParseError:return False
    if 'Exception' not in local(root.tag):return False
    for e in root.iter():
        code=e.attrib.get('exceptionCode','').lower()
        locator=e.attrib.get('locator','').lower()
        text=' '.join(e.itertext()).lower()
        if code in ('nosuchcoverage','invalidcoverageid'):return True
        if code in ('invalidsubsetting','invalidparametervalue') and ('time' in locator or 'time' in text):return True
    return False


def validate_grib(content,run,hour,box):
    import eccodes as ec
    import numpy as np
    count=0
    with tempfile.TemporaryFile() as stream:
        stream.write(content);stream.seek(0)
        while True:
            handle=ec.codes_grib_new_from_file(stream)
            if handle is None:break
            try:
                def get(key,default=None):
                    try:return ec.codes_get(handle,key)
                    except Exception:return default
                short=str(get('shortName','')).lower()
                recognized=short in ('tp','tirf') or (get('discipline')==0 and get('parameterCategory')==1 and get('parameterNumber') in (8,52,65))
                if not recognized or get('typeOfLevel')!='surface':raise ValueError('Le GRIB ne contient pas les RR au sol attendues.')
                actual=datetime.strptime(f'{int(get("dataDate")):08d}{int(get("dataTime")):04d}','%Y%m%d%H%M').replace(tzinfo=UTC)
                valid=datetime.strptime(f'{int(get("validityDate")):08d}{int(get("validityTime")):04d}','%Y%m%d%H%M').replace(tzinfo=UTC)
                if actual!=run or valid!=run+timedelta(hours=hour):raise ValueError('Réseau ou validité incorrect dans le GRIB.')
                if get('stepUnits') not in (1,'1','h') or get('startStep')!=hour-1 or get('endStep')!=hour:raise ValueError('Intervalle GRIB différent de la RR1h attendue.')
                if str(get('units','')).lower().replace(' ','') not in ('m','mm','kgm**-2','kgm^-2','kgm-2','kg/m2','kg/m^2'):raise ValueError('Unité de précipitation inattendue.')
                values=np.asarray(ec.codes_get_values(handle),dtype=float)
                good=np.isfinite(values)
                if get('bitmapPresent',0):good &=np.asarray(ec.codes_get_array(handle,'bitmap'))!=0
                if not np.any(good):raise ValueError('Le GRIB ne contient aucun point valide.')
                lon=(np.asarray(ec.codes_get_array(handle,'longitudes'))+180)%360-180
                lat=np.asarray(ec.codes_get_array(handle,'latitudes'))
                west,east,south,north=box
                if not np.any(good&(lon>=west-1e-5)&(lon<=east+1e-5)&(lat>=south-1e-5)&(lat<=north+1e-5)):raise ValueError('Aucun point valide dans le petit domaine demandé.')
                count+=1
            finally:ec.codes_release(handle)
    if count!=1:raise ValueError(f'Nombre de champs GRIB inattendu : {count}')


def event(state,events,model,run,milestone,status,beg,end,http_status='',detail=''):
    key=f'{model}|{stamp(run)}|{milestone}'
    row=state['targets'].setdefault(key,{'model':model,'run':stamp(run),'milestone':milestone,'first_test':beg,'first_present':None,'last_absent':None,'last_status':'','last_test':None,'precision_min':None})
    row['last_status']=status;row['last_test']=end
    if not row['first_present']:
        if status=='ABSENT':row['last_absent']=beg
        elif status=='PRESENT':
            row['first_present']=end
            row['precision_min']=(instant(end)-instant(row['last_absent'])).total_seconds()/60 if row['last_absent'] else None
    events.append({'test_start_utc':beg,'test_end_utc':end,'model':model,'run_utc':stamp(run),'milestone':milestone,'status':status,'http_status':http_status,'detail':detail})
    return row


def done(state,model,run,milestone):
    return bool(state['targets'].get(f'{model}|{stamp(run)}|{milestone}',{}).get('first_present'))


def candidates(now,hours,lookback):
    day=(now-timedelta(hours=lookback)).date();result=[]
    while day<=now.date():
        for h in hours:
            d=datetime(day.year,day.month,day.day,h,tzinfo=UTC)
            if now-timedelta(hours=lookback)<=d<=now:result.append(d)
        day+=timedelta(days=1)
    return result


def request(session,url,params,key):
    begin=stamp(datetime.now(UTC))
    try:
        response=session.get(url,params=params,headers={'apikey':key,'accept':'*/*'},timeout=(10,25))
        return begin,stamp(datetime.now(UTC)),response.status_code,response.content
    except Exception:
        # Ne pas journaliser d'URL ou d'exception susceptible de contenir un secret.
        return begin,stamp(datetime.now(UTC)),0,b''


def monitor(config,state):
    import requests
    key=os.environ.get('METEOFRANCE_API_KEY','').strip()
    if not key:raise ValueError('Secret METEOFRANCE_API_KEY absent ou vide.')
    session=requests.Session();events=[];now=datetime.now(UTC)
    runs=candidates(now,config['run_hours_utc'],config['lookback_hours'])
    box=config['bbox'];steps=config['milestones_hours']
    for model,options in config['models'].items():
        url=options['wcs_url'].rstrip('/')
        pending=[r for r in runs if not all(done(state,model,r,h) for h in steps)]
        if not pending:continue
        begin,end,http,body=request(session,url+'/GetCapabilities',BASE+[('language','fre')],key)
        try:
            if http!=200:raise ValueError('Catalogue inaccessible')
            available=catalogs(body)
        except (ValueError,ET.ParseError):
            for run in pending:
                for h in ['catalogue',*steps]:
                    if not done(state,model,run,h):event(state,events,model,run,h,'INDETERMINE',begin,end,http,'Catalogue non valide ; aucune absence déduite.')
            print(f'{model} : catalogue inaccessible ou non valide (HTTP {http}).')
            continue
        discovery=state.setdefault('observed_run_hours',{}).setdefault(model,[])
        discovery[:]=sorted(set(discovery)|{instant(s).hour for s in available})
        for run in pending:
            cid=available.get(stamp(run))
            if not done(state,model,run,'catalogue'):
                event(state,events,model,run,'catalogue','PRESENT' if cid else 'ABSENT',begin,end,http,'Catalogue RR1h, pas une preuve de téléchargeabilité.')
            if not cid:
                for h in steps:
                    if not done(state,model,run,h):event(state,events,model,run,h,'ABSENT',begin,end,http,'RR1h du réseau non annoncée dans le catalogue valide.')
                continue
            for h in steps:
                if done(state,model,run,h):continue
                valid=run+timedelta(hours=h);west,east,south,north=box
                params=BASE+[('coverageid',cid),('subset',f'time({valid:%Y-%m-%dT%H:%M:%SZ})'),('subset',f'lat({south},{north})'),('subset',f'long({west},{east})'),('format','application/wmo-grib')]
                time.sleep(config.get('request_spacing_seconds',.7))
                beg,ended,status,content=request(session,url+'/GetCoverage',params,key)
                result='INDETERMINE';detail='Erreur HTTP ou réponse non GRIB.'
                if status==200 and content.startswith(b'GRIB'):
                    try:
                        validate_grib(content,run,h,box);result='PRESENT';detail='GRIB RR1h validé : réseau, échéance, intervalle, domaine et valeurs.'
                    except Exception as exc:
                        detail='GRIB reçu mais validation échouée : '+str(exc)[:180]
                elif status not in (401,403,429) and 0<status<500 and exception_is_absent(content):
                    result='ABSENT';detail='Exception WCS explicite : coverage ou temps non disponible.'
                row=event(state,events,model,run,h,result,beg,ended,status,detail)
                print(f'{model} {run:%Y%m%d%H} H+{h:02d} : {result}'+(' (première détection)' if result=='PRESENT' else ''))
    return events


def export_report(state,folder):
    targets=sorted(state['targets'].values(),key=lambda r:(r['run'],r['model'],str(r['milestone'])))
    columns=['model','run','milestone','first_test','last_absent','first_present','precision_min','last_status','last_test']
    with (folder/'disponibilites.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(targets)
    tz=ZoneInfo('Europe/Paris');now=datetime.now(UTC)
    def label(s):
        if not s:return '—'
        d=instant(s);return d.strftime('%d/%m %H:%M UTC')+' / '+d.astimezone(tz).strftime('%H:%M %Z')
    rows=[];groups={}
    for r in reversed(targets):
        precision=r['precision_min'];first=r['first_present']
        if first and precision is not None:
            quality=f'{precision:.1f} min'+(' ⚠ >15 min' if precision>15 else '')
            status='Disponible'
            if isinstance(r['milestone'],int) and precision<=15:groups.setdefault((r['model'],instant(r['run']).hour,r['milestone']),[]).append((instant(first)-instant(r['run'])).total_seconds()/60)
        elif first:quality='Déjà disponible au premier test : heure réelle inconnue';status='Disponible, observation censurée'
        else:
            quality='—';status='Non observé sur la fenêtre de suivi' if now-instant(r['run'])>timedelta(hours=36) else r['last_status']
        cells=[r['model'],instant(r['run']).strftime('%d/%m/%Y %H UTC'),'Catalogue RR1h' if r['milestone']=='catalogue' else f'H+{r["milestone"]}',label(r['last_absent']),label(first),quality,status]
        rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in cells)+'</tr>')
    summary=[]
    def quantile(a,q):
        a=sorted(a);p=(len(a)-1)*q;i=int(p);f=p-i;return a[i]*(1-f)+a[min(i+1,len(a)-1)]*f
    for (model,run_hour,step),values in sorted(groups.items()):
        summary.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [model,f'{run_hour:02d} UTC',f'H+{step}',len(values),f'{quantile(values,.1):.0f}',f'{statistics.median(values):.0f}',f'{quantile(values,.9):.0f}'])+'</tr>')
    observed=' ; '.join(f'{m} : '+', '.join(f'{h:02d}' for h in hours)+' UTC' for m,hours in state.get('observed_run_hours',{}).items()) or 'Aucun catalogue exploitable pour le moment.'
    report='''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Disponibilité des modèles</title>
<style>body{font:14px system-ui;margin:20px;background:#f2f6fa;color:#19334b}h1{font-size:23px}h2{font-size:18px}.scroll{overflow:auto;max-height:70vh}table{border-collapse:collapse;background:white;width:100%}th,td{border:1px solid #d2dce6;padding:8px;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#dce9f5}p{max-width:1100px;line-height:1.5}</style>
<h1>Disponibilité des RR1h — ARO et ARO-CEP</h1><p>Mise à jour : __NOW__</p><p>Réseaux effectivement rencontrés dans les catalogues : __OBSERVED__</p>
<p>L’heure de disponibilité est comprise entre le dernier test d’absence et la première validation du GRIB. La largeur de cet intervalle est indiquée : un cron GitHub retardé peut dépasser 15 minutes. Les échecs techniques ne sont pas des preuves d’absence. Un réseau jamais annoncé peut ne pas être produit ; aucune heure de disponibilité n’est inventée.</p>
<h2>Délais observés après l’heure du réseau, en minutes</h2><p>Quantiles des premières détections réussies, uniquement lorsqu’une absence a été observée auparavant et lorsque la précision est de 15 minutes ou meilleure. Ces délais peuvent surestimer la disponibilité réelle dans la limite de la précision de chaque observation. Les sorties déjà disponibles au démarrage sont exclues.</p>
<div class="scroll"><table><thead><tr><th>Modèle</th><th>Réseau</th><th>Jalon</th><th>n</th><th>q10</th><th>Médiane</th><th>q90</th></tr></thead><tbody>__SUMMARY__</tbody></table></div>
<h2>Observations par réseau</h2><div class="scroll"><table><thead><tr><th>Modèle</th><th>Réseau UTC</th><th>Jalon</th><th>Dernière absence</th><th>Première réussite</th><th>Précision</th><th>État</th></tr></thead><tbody>__ROWS__</tbody></table></div></html>'''
    report=report.replace('__NOW__',html.escape(label(stamp(now)))).replace('__OBSERVED__',html.escape(observed)).replace('__SUMMARY__',''.join(summary)).replace('__ROWS__',''.join(rows))
    (folder/'disponibilites.html').write_text(report,encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path(__file__).resolve().parent/'config.json')
    parser.add_argument('--report-only',action='store_true')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    folder=args.config.parent/'data';folder.mkdir(exist_ok=True)
    path=folder/'etat.json';state=json.loads(path.read_text()) if path.exists() else {'targets':{},'observed_run_hours':{}}
    try:
        if not args.report_only:
            events=monitor(config,state)
            for day in sorted({e['test_start_utc'][:10] for e in events}):
                dest=folder/f'tests_{day}.csv';exists=dest.exists()
                with dest.open('a',encoding='utf-8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=list(events[0]));
                    if not exists:writer.writeheader()
                    writer.writerows(e for e in events if e['test_start_utc'].startswith(day))
            state['last_cycle']=stamp(datetime.now(UTC))
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
        export_report(state,folder)
    except Exception as exc:
        print('ERREUR : '+str(exc),file=sys.stderr);return 1
    print('Rapport : '+str(folder/'disponibilites.html'));return 0


if __name__=='__main__':
    raise SystemExit(main())
