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
    top = tops.main()
    BJ = BJs.main()
    Wal = Walmart.main()
    totalDF = pd.concat([aldi, top, BJ, Wal])
    return totalDF