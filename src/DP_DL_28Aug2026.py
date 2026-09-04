# -*- coding: utf-8 -*-
"""
Created on Wed Oct  2 13:00:45 2024
# Change Log
- 04Oct2024
Removed Timestep as a variable and now resample to hourly or daily based on defined ResSim path in the process USGS and process CWMS functions 
Added df = df.apply(pd.to_numeric,errors='coerce') to process CWMS function to catch odd format values downloading from CWMS 
- 04May2026
Removed SSL verify=False monkey-patch. Now uses pip-system-certs to pull certs from
the Windows certificate store. Run: pip install pip-system-certs
- 28Aug2026
USGS is decommissioning the legacy WaterServices API (waterservices.usgs.gov) in
Q1 2027 in favor of the modernized USGS Water Data API (api.waterdata.usgs.gov).
The dataretrieval.nwis module (nwis.get_record) is now deprecated and talks to the
legacy API that USGS is actively winding down, which is why downloads that worked
in May stopped working. Rewrote NWIS_dl to use dataretrieval.waterdata.get_continuous
/get_daily instead. This changes site IDs to the "USGS-#######" monitoring_location_id
format and returns a 'value' column instead of a numeric-named column, so
process_usgs_data was updated to read 'value' (with a fallback to the old numeric
column heuristic for anyone still on the legacy nwis module).
Requires dataretrieval>=1.3.0 (pip install -U dataretrieval).
Fixed: waterdata.get_continuous() has no skip_geometry argument (it never
returns geometry to begin with, unlike get_daily()) - removed it from the
'iv' branch of NWIS_dl, which was raising a TypeError.
Tried changing CWMS_Download's office_id from 'NWDP' to office.upper() to
match Modules/cwms_io.py in the Cowlitz_FF repo - didn't fix it. Reverted:
office_id='NWDP' back to match CAS_Unreg_FF/src/#DataDownload.py (the
Cowlitz initial data download script) exactly, and brought over that
script's SSL cert handling too. That script builds its own combined CA
bundle (certifi + the Windows ROOT store) and points REQUESTS_CA_BUNDLE
at it directly, instead of relying on pip-system-certs's global
monkey-patch, which doesn't survive every environment/upgrade (e.g. it can
get silently undone when certifi itself gets reinstalled/upgraded - which
`pip install -U dataretrieval` does). That silent TLS failure is the more
likely reason CWMS was still failing after the office_id change.

@author: g2encjer
"""
#%%

import os
import tempfile

import pandas as pd
from dataretrieval import waterdata
import datetime
import cwms
from pydsstools.heclib.dss import HecDss
from pydsstools.core import TimeSeriesContainer
import numpy as np
import time
import pdb
import requests

import ssl
import certifi

# --- SSL Certificate Setup ---
# Build a combined CA bundle (public CAs from certifi + the Windows ROOT
# store) and point REQUESTS_CA_BUNDLE at it so requests/cwms/dataretrieval
# trust USACE's internally-issued certs. This must run before any network
# calls (CWMS_Download, NWIS_dl) below. On non-Windows platforms,
# ssl.enum_certificates doesn't exist and this falls back to certifi alone.
pem_path = os.path.join(tempfile.gettempdir(), "corp_plus_certifi.pem")


def build_windows_ca_bundle(target_pem: str) -> str:
    base_bundle = certifi.where()
    with open(base_bundle, "rb") as src, open(target_pem, "wb") as dst:
        dst.write(src.read())
        try:
            for cert_tuple in ssl.enum_certificates("ROOT"):
                der_bytes = cert_tuple[0]
                pem_str = ssl.DER_cert_to_PEM_cert(der_bytes)
                dst.write(pem_str.encode("ascii"))
        except AttributeError:
            print("[WARNING] ssl.enum_certificates not available; using certifi only.")
        except Exception as e:
            print(f"[WARNING] Error reading Windows ROOT store: {e}")
    return target_pem


if not os.path.exists(pem_path):
    try:
        bundle_path = build_windows_ca_bundle(pem_path)
        print(f"[INFO] Built combined CA bundle: {bundle_path}")
    except Exception as e:
        print(f"[WARNING] Failed to build combined CA bundle: {e}")
        bundle_path = certifi.where()
else:
    bundle_path = pem_path

os.environ["REQUESTS_CA_BUNDLE"] = bundle_path
print(f"[INFO] Using CA bundle: {bundle_path}")
# --- End SSL Setup ---


RequiredRecordsDictPath = r'../data/RequiredRecordsDictNWP_Short.csv'

# Start and end date, probably water year
startDate = '2023-12-25'
endDate = '2024-1-02'
# DSS file with final results
ObsDataWrite = 'obsData' 


#Functions
def NWIS_dl(sites_dict, service, startDate, endDate, parameterCD):
    """
    Downloads USGS data via the modernized USGS Water Data API
    (dataretrieval.waterdata), which replaces the legacy WaterServices API
    formerly accessed through dataretrieval.nwis.get_record.

    service: 'iv' for continuous/instantaneous values, 'dv' for daily values
    (reported as the daily mean, statistic_id '00003').
    """
    NWIS = {}
    # ISO 8601 interval covering the full start/end days, as required by the
    # 'time' parameter of the waterdata getters. Parse with pandas first so a
    # loosely-formatted date (e.g. '2024-1-02') still produces a valid,
    # zero-padded RFC3339 string instead of getting interpolated as-is.
    start_dt = pd.to_datetime(startDate)
    end_dt = pd.to_datetime(endDate) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    time_range = f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for site, name in sites_dict.items():
        # The new API keys sites as "USGS-#######" (agency-siteno) rather than
        # the bare site number used by the old nwis module.
        monitoring_location_id = site if str(site).upper().startswith('USGS-') else f"USGS-{site}"
        try:
            if service == 'iv':
                # get_continuous has no skip_geometry kwarg - it never returns
                # geometry to begin with.
                data, _ = waterdata.get_continuous(
                    monitoring_location_id=monitoring_location_id,
                    parameter_code=parameterCD,
                    time=time_range,
                )
            elif service == 'dv':
                data, _ = waterdata.get_daily(
                    monitoring_location_id=monitoring_location_id,
                    parameter_code=parameterCD,
                    statistic_id='00003',
                    time=time_range,
                    skip_geometry=True,
                )
            else:
                raise ValueError(f"Unsupported service '{service}'. Use 'iv' or 'dv'.")
            if data.empty:
                print(f"Downloaded data for {site} is empty.")
            else:
                data['time'] = pd.to_datetime(data['time'])
                data = data.set_index('time').sort_index()
                NWIS[name] = data
        except Exception as e:
            print(f"Failed to download data for {site}: {e}")
    return NWIS

def CWMS_Download(sites_dict, StartDate, EndDate, office='nws'):
    # Convert StartDate and EndDate to datetime objects
    StartDate = pd.to_datetime(StartDate)
    EndDate = pd.to_datetime(EndDate)

    # Initialize CWMS API session
    apiRoot = "https://wm." + office + ".ds.usace.army.mil:8243/nwdp-data/"
    api = cwms.api.init_session(api_root=apiRoot)

    # Initialize empty dictionary to store data for each tsid
    CWMS_data = {}
    # Loop through each tsid
    for site, name in sites_dict.items():
        try:
            # Try to download data and store the dataframe for the tsid
            data = cwms.get_timeseries(site, office_id='NWDP', begin=StartDate, end=EndDate).df
            # Check if the data is empty
            if data.empty:
                print(f"Downloaded data for {site} is empty.")
            else:
                CWMS_data[name] = data
        except Exception as e:
            # Print the failed tsid and the error message
            print(f"Failed to download data for {site}: {e}")
    return CWMS_data

def process_usgs_data(DataDict):
    # Create an empty list to store the summary stats
    results = []
    for df_name, df in DataDict.items():
        if not df.empty and df.shape[1] > 0:
            # The modernized waterdata API returns the observation in a 'value'
            # column. Fall back to the old numeric-named-column heuristic for
            # anyone still downloading via the legacy dataretrieval.nwis module.
            if 'value' in df.columns:
                df = df['value'].copy()
            else:
                valid_columns = [col for col in df.columns if col.replace('_', '').isdigit()]
                if len(valid_columns) == 1:
                    df = df[valid_columns[0]].copy()
                elif len(valid_columns) > 1:
                    print(f"Warning: Multiple valid columns found in {df_name}. Using the first one: {valid_columns[0]}")
                    df = df[valid_columns[0]].copy()
                else:
                    raise ValueError("No valid columns found that contain only numbers or underscores.")
            df = pd.to_numeric(df, errors='coerce')
            df[df < -9000] = np.nan
            df[df==-902]=np.nan
            df[df==-901]=np.nan
            df = df.dropna()
            #Create SummaryStats
            first_timestamp = df.index.min().strftime('%Y-%m-%d %H:%M')
            last_timestamp = df.index.max().strftime('%Y-%m-%d %H:%M')
            # Calculate the maximum gap between consecutive timestamps
            time_diffs = df.index.to_series().diff().dropna()
            max_gap = time_diffs.max()
            max_gap_hours=max_gap.total_seconds()/3600.0
            # Append the results for this dataframe to the list
            results.append({
                'DataFrame': df_name,
                'First Timestamp': first_timestamp,
                'Last Timestamp': last_timestamp,
                'Max Gap': max_gap,
                'Max Gap Hours': max_gap_hours
            })
            # Resample to hourly and replace nan with dss nan
            if '1HOUR' in df_name:
                t = 'h'
            elif '1DAY' in df_name:
                t = 'D'
            else:
                print('timestep of ResSim path not 1HOUR or 1DAY')
            df = df.resample(t).mean()
            df = df.fillna(-902)
            DataDict[df_name] = df
        else:
            print(f"DataFrame {df_name} is either empty or does not have any columns.")
    results_df = pd.DataFrame(results)
    return results_df

def process_cwms_data(DataDict):
    # Create an empty list to store the summary stats
    results = []
    for df_name, df in DataDict.items():
        # Set 'date-time' as index if it exists
        if 'date-time' in df.columns:
            df = df.set_index('date-time')
            DataDict[df_name] = df
        if not df.empty and df.shape[1] > 0:
            # Keep only the first column and clean missing value standins
            df = df.iloc[:, [0]].copy()
            df = df.apply(pd.to_numeric,errors='coerce')
            df[df < -9000] = np.nan
            df[df==-902]=np.nan
            df[df==-901]=np.nan
            df = df.dropna()
            #Create SummaryStats
            first_timestamp = df.index.min().strftime('%Y-%m-%d %H:%M')
            last_timestamp = df.index.max().strftime('%Y-%m-%d %H:%M')
            # Calculate the maximum gap between consecutive timestamps
            time_diffs = df.index.to_series().diff().dropna()
            max_gap = time_diffs.max()
            max_gap_hours = max_gap.total_seconds()/3600.0
            # Append the results for this dataframe to the list
            results.append({
                'DataFrame': df_name,
                'First Timestamp': first_timestamp,
                'Last Timestamp': last_timestamp,
                'Max Gap': max_gap,
                'Max Gap Hours' : max_gap_hours
            })
            # Resample to hourly or daily, fill missing value standins
            if '1HOUR' in df_name:
                t = 'h'
            elif '1DAY' in df_name:
                t = 'D'
            else:
                print('timestep of ResSim path not 1HOUR or 1DAY')
            df = df.resample(t).mean()
            df = df.fillna(-902)
            DataDict[df_name] = df
        else:
            print(f"DataFrame {df_name} is either empty or does not have any columns.")
    # Convert the list of results into a DataFrame
    results_df = pd.DataFrame(results)
    return results_df

def write_to_dss(dss_file, DataDict):
    for pathname, df in DataDict.items():
        # Debugging statements
        print(f"Processing pathname: {pathname}")
        # Ensure df is a DataFrame
        if isinstance(df, pd.Series):
            df = df.to_frame()
        # Create the time series container
        tsc = TimeSeriesContainer()
        tsc.pathname = pathname
        tsc.startDateTime = str(df.index[0])
        tsc.numberValues = df.shape[0]
        # Check if df has the expected structure
        if df.shape[1] > 0:
            tsc.values = df.iloc[:, 0].copy().to_numpy()
        else:
            print(f"DataFrame {pathname} does not have any columns.")
            continue
        tsc.interval = 1  # Assuming this is the interval
        # Set units based on the path
        if "ELEV" in pathname:
            tsc.units = "FEET"
        elif "FLOW" in pathname:
            tsc.units = "CFS"
        else:
            tsc.units = 'Unknown'
            print('Not Flow or Elev!')
        # Set the type
        tsc.type = "INST-VAL"  # Assuming this is always the type
        # Write the data to the DSS file to the out folder

        with HecDss.Open(dss_file, version=6) as fid:
            fid.put_ts(tsc)
        
#%%
# Read in the CSV that maps required Damages Prevented inputs with download keys (USGS or CWMS).
# Create this csv with the CreateRequiredRecordsDict.py script.
RequiredRecordsDict = pd.read_csv(RequiredRecordsDictPath)

# Create the required USGS and CWMS dictionaries. USGS needs one for Elev and one for Flow
# because the parameterCD code for each is different.
USGS_df = RequiredRecordsDict[RequiredRecordsDict['Source']=='USGS']

USGS_Elev_df = USGS_df[USGS_df['ResSimPath'].str.contains('ELEV', na=False)]
USGS_Elev_dict = dict(zip(USGS_Elev_df['Download_Key'],USGS_Elev_df['ResSimPath']))

USGS_Flow_df = USGS_df[USGS_df['ResSimPath'].str.contains('FLOW', na=False)]
USGS_Flow_dict = dict(zip(USGS_Flow_df['Download_Key'],USGS_Flow_df['ResSimPath']))

CWMS_df = RequiredRecordsDict[RequiredRecordsDict['Source']=='CWMS']
CWMS_dict = dict(zip(CWMS_df['Download_Key'],CWMS_df['ResSimPath']))

#%%
# Download the data. All data is downloaded as instant. The processing later makes it hourly
# or daily. You could also set the service to 'dv' for daily if you don't need hourly.

USGS_Elev_Data_Dict = NWIS_dl(sites_dict = USGS_Elev_dict, service = 'iv', startDate = startDate, endDate = endDate, parameterCD = '62614')

USGS_Flow_Data_Dict = NWIS_dl(sites_dict = USGS_Flow_dict, service = 'iv', startDate = startDate, endDate = endDate, parameterCD = '00060')

CWMS_Data_Dict = CWMS_Download(sites_dict=CWMS_dict, StartDate = startDate, EndDate = endDate)

#%%
# Process Data and create summary stats - this process gets rid of all the metadata that comes
# in with the data, and also creates summary stats that are written to a csv.
CWMS_Summary_Stats = process_cwms_data(CWMS_Data_Dict)
USGS_Flow_Summary_Stats = process_usgs_data(USGS_Flow_Data_Dict)
USGS_Elev_Summary_Stats = process_usgs_data(USGS_Elev_Data_Dict)

#%%
Combined_Summary_Stats = pd.concat([CWMS_Summary_Stats,USGS_Flow_Summary_Stats,USGS_Elev_Summary_Stats], ignore_index= True)
Combined_Summary_Stats['Max Gap Hours'] = Combined_Summary_Stats['Max Gap Hours'].astype(float).round(2)
Combined_Summary_Stats.sort_values(by='Max Gap Hours', ascending=False, inplace=True)
Combined_Summary_Stats.reset_index(drop=True, inplace=True)
Combined_Summary_Stats.to_csv('../out/Combined_Summary_Stats.csv', index=None)

#%%
# Write obsdata. This writes the final dss file your ResSim alternatives will reference.
# Write to out folder
write_to_dss(dss_file = '../out/' + ObsDataWrite, DataDict=USGS_Flow_Data_Dict)
write_to_dss(dss_file = '../out/' + ObsDataWrite, DataDict=USGS_Elev_Data_Dict)
write_to_dss(dss_file = '../out/' + ObsDataWrite, DataDict=CWMS_Data_Dict)

# %%