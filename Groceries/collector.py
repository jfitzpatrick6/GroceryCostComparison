import pandas as pd
import re
import time

import aldis
import BJs
import tops
import Walmart


def getData():
    """Calls all of the main functions for each scrape, and returns a single Dataframe."""
    with open("1.env", 'r') as f:
        env = f.read()
    env = env.split('\n')
    aldi = aldis.main(env[1].split("=")[1])
    aldi['Store'] = 'Aldis'
    top = tops.main(env[0].split("=")[1])
    top['Store'] = 'Tops'
    BJ = BJs.main(env[2].split("=")[1])
    BJ['Store'] = 'BJs'
    Wal = Walmart.main(env[3].split("=")[1])
    Wal['Store'] = 'Walmart'
    totalDF = pd.concat([aldi, top, BJ, Wal], ignore_index=True)
    totalDF['Datetime'] = pd.Timestamp.now()
    return totalDF

def main():
    data = getData()
    timestamp = pd.Timestamp.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"Grocery Report {timestamp}.csv"
    data.to_csv(filename, index=False)

main()