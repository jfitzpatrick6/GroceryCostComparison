import pandas as pd
import re
import time

import aldis
import BJs
import tops
import Walmart


def getData():
    """Calls all of the main functions for each scrape, and returns a single Dataframe."""
    aldi = aldis.main()
    top = tops.main("102")
    BJ = BJs.main()
    Wal = Walmart.main()
    totalDF = pd.concat([aldi, top, BJ, Wal], ignore_index=True)
    totalDF['Datetime'] = pd.Timestamp.now()
    return totalDF

def main():
    data = getData()
    timestamp = pd.Timestamp.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"Grocery Report {timestamp}.csv"
    data.to_csv(filename, index=False)

main()