# EZSheets
# By Al Sweigart al@inventwithpython.com

# IMPORTANT NOTE: This module has not been stress-tested for performance
# and should not be considered "thread-safe" if multiple users are

import pickle, re, collections, time
import os.path
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

__version__ = '0.0.2'

#SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE = None
IS_INITIALIZED = False

DEFAULT_NEW_ROW_COUNT = 1000  # This is the Google Sheets default for a new Sheet.
DEFAULT_NEW_COLUMN_COUNT = 26 # This is the Google Sheets default for a new Sheet.
DEFAULT_FROZEN_ROW_COUNT = 0
DEFAULT_FROZEN_COLUMN_COUNT = 0
DEFAULT_HIDE_GRID_LINES = False
DEFAULT_ROW_GROUP_CONTROL_AFTER = False
DEFAULT_COLUMN_GROUP_CONTROL_AFTER = False

from ezsheets.colorvalues import COLORS

# Quota throttling:
_READ_REQUESTS = collections.deque()
_WRITE_REQUESTS = collections.deque()
READ_QUOTA = 50 # 50 reads per 100 seconds
WRITE_QUOTA = 50 # 50 writes per 100 seconds

"""
Features to add:
- delete spreadsheets
- download as csv/excel/whatever
"""


# Sample spreadsheet id: 16RWH9XBBwd8pRYZDSo9EontzdVPqxdGnwM5MnP6T48c

def _logWriteRequest():
    """
    Logs a write request to the `_WRITE_REQUESTS` deque. This function should be
    called whenever a Google Sheets write request is made. It will also throttle
    requests based on the quota in WRITE_QUOTA.
    """
    _WRITE_REQUESTS.append(time.time())
    while _WRITE_REQUESTS[0] < time.time() - 100:
        _WRITE_REQUESTS.popleft() # Get rid of all entries older than 100 seconds.

    while len(_WRITE_REQUESTS) > WRITE_QUOTA: # pragma: no cover
        time.sleep(1)
        while _WRITE_REQUESTS[0] < time.time() - 100:
            _WRITE_REQUESTS.popleft() # Get rid of all entries older than 100 seconds.

def _logReadRequests():
    """
    Logs a read request to the `_READ_REQUESTS` deque. This function should be
    called whenever a Google Sheets read request is made. It will also throttle
    requests based on the quota in READ_QUOTA.
    """
    _READ_REQUESTS.append(time.time())
    while _READ_REQUESTS[0] < time.time() - 100:
        _READ_REQUESTS.popleft() # Get rid of all entries older than 100 seconds

    while len(_READ_REQUESTS) > READ_QUOTA: # pragma: no cover
        time.sleep(1)
        while _READ_REQUESTS[0] < time.time() - 100:
            _READ_REQUESTS.popleft() # Get rid of all entries older than 100 seconds


class EZSheetsException(Exception):
    """
    This class exists for this module to raise for EZSheets-specific problems.
    """
    pass


class Spreadsheet():
    """
    This class represents a Spreadsheet on Google Sheets. Spreadsheets can
    contain one or more sheets, also called worksheets.
    """
    def __init__(self, spreadsheetId):
        """
        Initializer for Spreadsheet objects.

        :param spreadsheetId: The ID or URL of the spreadsheet on Google Sheets. E.g. `'https://docs.google.com/spreadsheets/d/10tRbpHZYkfRecHyRHRjBLdQYoq5QWNBqZmH9tt4Tjng/edit#gid=0'` or `'10tRbpHZYkfRecHyRHRjBLdQYoq5QWNBqZmH9tt4Tjng'`
        """
        if not IS_INITIALIZED: init() # Initialize this module if not done so already.

        self._spreadsheetId = getIdFromUrl(spreadsheetId)
        self.sheets = ()
        self.refresh()

    def refresh(self):
        """
        Updates the local Spreadsheet and Sheet objects with the current state
        of the spreadsheet and sheets on Google Plus.
        """
        request = SERVICE.spreadsheets().get(spreadsheetId=self._spreadsheetId)
        response = request.execute(); _logReadRequests()

        self._title = response['properties']['title']
        
        sheetIDS = {}
        for i, sh in enumerate(self.sheets):
            sheetIDS[sh.sheetId] = i

        # Update/create Sheet objects:
        replacementSheetsAttr = [] # We will replace self.sheets with this list.
        for i, sheetInfo in enumerate(response['sheets']):
            sheetId = sheetInfo['properties']['sheetId']

            if sheetId in sheetIDS:
                existingSheetIndex = sheetIDS[sheetId]
            else:
                existingSheetIndex = None
            
            if existingSheetIndex is not None:
                # If the sheet has been previously loaded, reuse that Sheet object:
                replacementSheetsAttr.append(self.sheets[existingSheetIndex])
                self.sheets[existingSheetIndex]._refreshPropertiesWithSheetPropertiesDict(sheetInfo['properties'])
                self.sheets[existingSheetIndex]._refreshData()
            else:
                # If the sheet hasn't been seen before, create a new Sheet object:
                replacementSheetsAttr.append(Sheet(self, sheetId)) # TODO - would be nice to reuse the info in `response` for this instead of letting the ctor make another request, but this isn't that important.

        del sheetIDS
        self.sheets = tuple(replacementSheetsAttr) # Make sheets attribute an immutable tuple.


    def __getitem__(self, key):
        """
        TODO
        """
        try:
            i = self.sheetTitles.index(key)
            return self.sheets[i]
        except ValueError:
            pass # Do nothing if the title isn't found.


        if isinstance(key, int) and (-len(self.sheets) <= key < len(self.sheets)):
            return self.sheets[key]
        if isinstance(key, slice):
            return self.sheets[key]

        raise KeyError('key must be an int between %s and %s or a str matching a title: %r' % (-(len(self.sheets)), len(self.sheets) - 1, self.sheetTitles))

    def __delitem__(self, key):
        """
        TODO
        """
        if isinstance(key, (int, str)):
            # Key is an int index or a str title.
            self[key].delete()
        elif isinstance(key, slice):
            # TODO - there's got to be a better way to do this.
            start = key.start if key.start is not None else 0
            stop  = key.stop  if key.stop  is not None else len(self.sheets)
            step  = key.step  if key.step  is not None else 1

            if start < 0 or stop < 0:
                return # When deleting list items with a slice, a negative start or stop results in a no-op. I'll mimic that behavior here.

            indexesToDelete = [i for i in range(start, stop, step) if i >= 0 and i < len(self.sheets)] # Don't include invalid or negative indexes.
            if len(indexesToDelete) == len(self.sheets):
                raise ValueError('Cannot delete all sheets; spreadsheets must have at least one sheet')

            if indexesToDelete[0] < indexesToDelete[-1]:
                indexesToDelete.reverse() # We want this is descending order.

            for i in indexesToDelete:
                self.sheets[i].delete()

        else:
            raise TypeError('key must be an int index, str sheet title, or slice object, not %r' % (type(key).__name__))

    def __len__(self):
        """
        returns the number of sheets in the spreadsheet object.
        
        :returns: int - length of self.sheets
        """
        return len(self.sheets)

    def __iter__(self):
        """
        TODO
        """
        return iter(self.sheets)

    @property
    def spreadsheetId(self):
        """
        returns the `spreadsheetId` of the Spreadsheet object.
        
        :returns: int - google spreadsheetId of the spreadsheet object.
        """
        return self._spreadsheetId

    @property
    def sheetTitles(self):
        """
        returns the titles of all the sheets in the Spreadsheet object.
        
        :returns: tuple of strings - All the sheet titles in the Spreadsheet 
        Object.
        """
        return tuple([sheet.title for sheet in self.sheets])

    def __str__(self):
        """
        returns a string representation of the Spreadsheet object, it does not
        return information about the underlying sheet objects.
        
        :returns: String - name, title, and number of sheets
        """
        return '<%s title="%s", %d sheets>' % (type(self).__name__, self.title, len(self.sheets))

    def __repr__(self):
        """
        TODO
        """
        return '%s(spreadsheetId=%r)' % (type(self).__name__, self.spreadsheetId)

    @property
    def title(self):
        """
        returns the title of the Spreadsheet object.
        
        :returns: String - title of the Spreadsheet object
        """
        return self._title

    @title.setter
    def title(self, value):
        value = str(value)
        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheetId,
        body={
            'requests': [{'updateSpreadsheetProperties': {'properties': {'title': value},
                                                          'fields': 'title'}}]})
        request.execute(); _logWriteRequest()
        self._title = value


    def addSheet(self, title='', index=None, columnCount=DEFAULT_NEW_COLUMN_COUNT, rowCount=DEFAULT_NEW_ROW_COUNT):
        """
        TODO
        """
        if index is None:
            # Set the index to make this new sheet be the last sheet:
            index = len(self.sheets)

        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheetId,
        body={
            'requests': [{'addSheet': {'properties': {'title': title, 'index': index}}}]})
        request.execute(); _logWriteRequest()

        self.refresh()
        self.sheets[index].resize(columnCount, rowCount)
        return self.sheets[index]



class Sheet():
    """
    TODO
    """
    def __init__(self, spreadsheet, sheetId):
        """
        TODO
        """
        #if not IS_INITIALIZED: init() # Initialize this module if not done so already. # This line might not be needed? Sheet objects can only exist when you've already made a Spreadsheet object.

        # Set the properties of this sheet
        self._spreadsheet = spreadsheet
        self._sheetId = sheetId
        self._cells = {} # To ease development, internally the local copy of the sheet data is stored in a dict with 1-based (column, row) keys.
        self.refresh()

    # Set up the read-only attributes.
    @property
    def spreadsheet(self):
        """
            Returns a reference to the Spreadsheet object this sheet belongs to
            
            :returns: Object - Spreadsheet Object
        """
        return self._spreadsheet

    @property
    def title(self):
        """
            Returns the title of the sheet
            
            :returns: String - title of the sheet
        """
        return self._title

    @title.setter
    def title(self, value):
        value = str(value)
        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet.spreadsheetId,
        body={
            'requests': [{'updateSheetProperties': {'properties': {'sheetId': self._sheetId,
                                                                   'title': value},
                                                    'fields': 'title'}}]})
        request.execute(); _logWriteRequest()
        self._title = value


    @property
    def tabColor(self):
        """
        TODO
        """
        return self._tabColor

    @tabColor.setter
    def tabColor(self, value):
        tabColorArg = _getTabColorArg(value)

        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet.spreadsheetId,
        body={
            'requests': [{'updateSheetProperties': {'properties': {'sheetId': self._sheetId,
                                                                   'tabColor': tabColorArg},
                                                    'fields': 'tabColor'}}]})
        request.execute(); _logWriteRequest()
        self._tabColor = tabColorArg


    @property
    def index(self):
        """
        TODO
        """
        return self._index


    @index.setter
    def index(self, value):
        if value == self._index:
            return # No change needed.

        if not isinstance(value, int):
            raise TypeError('indices must be integers, not %s' % (type(value).__name__))

        if value < 0: # Handle negative indexes the way Python lists do.
            if value < -len(self.spreadsheet.sheets):
                raise IndexError('%r is out of range (-1 to %d)' % (value, -len(self.spreadsheet.sheets)))
            value = len(self.spreadsheet.sheets) + value # convert this negative index into its corresponding positive index
        if value >= len(self.spreadsheet.sheets):
            raise IndexError('%r is out of range (0 to %d)' % (value, len(self.spreadsheet.sheets) - 1))

        # Update the index:
        if value > self._index:
            value += 1 # Google Sheets uses "before the move" indexes, which is confusing and I don't want to do it here.

        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet._spreadsheetId,
        body={
            'requests': [{'updateSheetProperties': {'properties': {'sheetId': self._sheetId,
                                                                   'index': value},
                                                    'fields': 'index'}}]})
        request.execute(); _logWriteRequest()

        self._spreadsheet.refresh() # Update the spreadsheet's tuple of Sheet objects to reflect the new order.
        #self._index = self._spreadsheet.sheets.index(self) # Update the local Sheet object's index.


    def __eq__(self, other):
        if not isinstance(other, Sheet):
            return False
        return self._sheetId == other._sheetId

    @property
    def sheetId(self):
        return self._sheetId


    @property
    def rowCount(self):
        return self._rowCount

    @rowCount.setter
    def rowCount(self, value):
        # Validate arguments:
        if not isinstance(value, int):
            raise TypeError('value arg must be an int, not %s' % (type(value).__name__))
        if value < 1:
            raise TypeError('value arg must be a positive nonzero int, not %r' % (value))
        if value <= self._frozenRowCount:
            raise ValueError('You cannot have all rows on the sheet frozen (sheet %r has %s frozen rows)' % (self.title, self._frozenRowCount))

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._rowCount = value        # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def columnCount(self):
        return self._columnCount


    @columnCount.setter
    def columnCount(self, value):
        # Validate arguments:
        if not isinstance(value, int):
            raise TypeError('value arg must be an int, not %s' % (type(value).__name__))
        if value < 1:
            raise TypeError('value arg must be a positive nonzero int, not %r' % (value))
        if value <= self._frozenColumnCount:
            raise ValueError('You cannot have all columns on the sheet frozen (sheet %r has %s frozen columns)' % (self.title, self._frozenColumnCount))

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._columnCount = value     # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def frozenRowCount(self):
        return self._frozenRowCount


    @frozenRowCount.setter
    def frozenRowCount(self, value):
        # Validate arguments:
        if not isinstance(value, int):
            raise TypeError('value arg must be an int, not %s' % (type(value).__name__))
        if value < 1:
            raise TypeError('value arg must be a positive nonzero int, not %r' % (value))
        if value >= self._rowCount:
            raise ValueError('You cannot freeze all rows on the sheet (sheet %r has %s rows)' % (self.title, self._rowCount))

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._frozenRowCount = value  # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def frozenColumnCount(self):
        return self._frozenColumnCount


    @frozenColumnCount.setter
    def frozenColumnCount(self, value):
        # Validate arguments:
        if not isinstance(value, int):
            raise TypeError('value arg must be an int, not %s' % (type(value).__name__))
        if value < 1:
            raise TypeError('value arg must be a positive nonzero int, not %r' % (value))
        if value >= self._columnCount:
            raise ValueError('You cannot freeze all columns on the sheet (sheet %r has %s columns)' % (self.title, self._columnCount))

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._frozenColumnCount = value  # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def hideGridlines(self):
        return self._hideGridlines


    @hideGridlines.setter
    def hideGridlines(self, value):
        value = bool(value)

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._hideGridlines = value   # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def rowGroupControlAfter(self):
        return self._rowGroupControlAfter


    @rowGroupControlAfter.setter
    def rowGroupControlAfter(self, value):
        value = bool(value)

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._rowGroupControlAfter = value # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    @property
    def columnGroupControlAfter(self):
        return self._columnGroupControlAfter


    @columnGroupControlAfter.setter
    def columnGroupControlAfter(self, value):
        value = bool(value)

        self.refresh() # Retrieve up-to-date grid properties from Google Sheets.
        self._columnGroupControlAfter = value # Change local grid property.
        self._updateGridProperties()  # Upload grid properties to Google Sheets.


    def __str__(self):
        return '<%s title=%r, sheetId=%r, rowCount=%r, columnCount=%r>' % (type(self).__name__, self._title, self._sheetId, self._rowCount, self._columnCount)


    def __repr__(self):
        return '%s(sheetId=%r, title=%r, rowCount=%r, columnCount=%r)' % (type(self).__name__, self.sheetId, self._title, self._rowCount, self._columnCount)


    def get(self, *args):
        # TODO!!!! Add a switch or a mode or something so that all the ezsheets functions call refresh() before running.
        if len(args) == 2: # args are column, row like (2, 5)
            column, row = args
        elif len(args) == 1: # args is a string of a grid cell like ('B5',)
            column, row = convertToColumnRowInts(args[0])
        else:
            raise TypeError("get() takes one or two arguments, like ('A1',) or (2, 5)")

        if not isinstance(column, int):
            raise TypeError('column indices must be integers, not %s' % (type(column).__name__))
        if not isinstance(row, int):
            raise TypeError('row indices must be integers, not %s' % (type(row).__name__))
        if column < 1 or row < 1:
            raise IndexError('Column %s, row %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index. Negative indices are not supported by ezsheets.' % (column, row))

        return self._cells.get((column, row), '')

    """
    def getAllRows(self):
        rows = []
        for rowNum in range(1, self._rowCount + 1):
            row = []
            for colNum in range(1, self._columnCount + 1):
                row.append(self._cells.get((colNum, rowNum), ''))
            rows.append(row)
        return rows


    def getAllColumns(self):
        cols = []
        for colNum in range(1, self._columnCount + 1):
            col = []
            for rowNum in range(1, self._rowCount + 1):
                col.append(self._cells.get((colNum, rowNum), ''))
            cols.append(col)
        return cols
    """

    def getRow(self, rowNum):
        # NOTE: getRow() and getCol() do not support negative indexes.
        if not isinstance(rowNum, int):
            raise TypeError('rowNum indices must be integers, not %s' % (type(rowNum).__name__))
        if rowNum < 1:
            raise IndexError('Row %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index.' % (rowNum))

        row = []
        for colNum in range(1, self._columnCount + 1):
            row.append(self._cells.get((colNum, rowNum), ''))
        return row


    def getRows(self, startRow=1, stopRow=None):
        # Validate arguments:
        if stopRow is None:
            stopRow = self._rowCount + 1
        if not isinstance(startRow, int):
            raise TypeError('startRow arg must be an int, not %s' % (type(startRow).__name__))
        if startRow < 1:
            raise ValueError('startRow arg must be at least 1, not %s' % (startRow))
        if not isinstance(stopRow, int):
            raise TypeError('stopRow arg must be an int, not %s' % (type(stopRow).__name__))
        if stopRow < 1:
            raise ValueError('stopRow arg must be at least 1, not %s' % (stopRow))

        # Get rows by calling getRow():
        return [self.getRow(rowNum) for rowNum in range(startRow, stopRow)]


    def __contains__(self, item):
        pass


    def getColumn(self, colNum):
        # NOTE: getRow() and getCol() do not support negative indexes.
        if isinstance(colNum, str):
            colNum = getColumnNumber(colNum)

        if not isinstance(colNum, int):
            raise TypeError('colNum indices must be integers, not %s' % (type(colNum).__name__))
        if colNum < 1:
            raise IndexError('Column %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index.' % (colNum))

        column = []
        for rowNum in range(1, self._rowCount + 1):
            column.append(self._cells.get((colNum, rowNum), ''))
        return column


    def getColumns(self, startColumn=1, stopColumn=None):
        # Validate arguments:
        if stopColumn is None:
            stopColumn = self._columnCount + 1
        if not isinstance(startColumn, int):
            raise TypeError('startColumn arg must be an int, not %s' % (type(startColumn).__name__))
        if startColumn < 1:
            raise ValueError('startColumn arg must be at least 1, not %s' % (startColumn))
        if not isinstance(stopColumn, int):
            raise TypeError('stopColumn arg must be an int, not %s' % (type(stopColumn).__name__))
        if stopColumn < 1:
            raise ValueError('stopColumn arg must be at least 1, not %s' % (stopColumn))

        # Get columns by calling getColumn():
        return [self.getColumn(colNum) for colNum in range(startColumn, stopColumn)]


    def refresh(self):
        self._refreshProperties()
        self._refreshData()


    def _refreshProperties(self):
        # Get all the sheet properties:
        response = SERVICE.spreadsheets().get(spreadsheetId=self._spreadsheet._spreadsheetId).execute(); _logReadRequests()

        for sheetDict in response['sheets']:
            if sheetDict['properties']['sheetId'] == self._sheetId: # Find this sheet in the returned spreadsheet json data.
                self._refreshPropertiesWithSheetPropertiesDict(sheetDict['properties'])


    def _refreshPropertiesWithSheetPropertiesDict(self, sheetPropsDict):
        self._title = sheetPropsDict['title']
        self._index = sheetPropsDict['index']
        self._tabColor = _getTabColorArg(sheetPropsDict.get('tabColor')) # Set to None if there is no tabColor.

        # These attrs we don't have properties for yet, I'm not sure if we'll keep them:
        self._sheetType   = sheetPropsDict.get('sheetType')
        self._hidden      = sheetPropsDict.get('hidden')
        self._rightToLeft = sheetPropsDict.get('rightToLeft')

        gridProps = sheetPropsDict['gridProperties']
        self._rowCount                = gridProps.get('rowCount', DEFAULT_NEW_ROW_COUNT)
        self._columnCount             = gridProps.get('columnCount', DEFAULT_NEW_COLUMN_COUNT)
        self._frozenRowCount          = gridProps.get('frozenRowCount', DEFAULT_FROZEN_ROW_COUNT)
        self._frozenColumnCount       = gridProps.get('frozenColumnCount', DEFAULT_FROZEN_COLUMN_COUNT)
        self._hideGridlines           = gridProps.get('hideGridlines', DEFAULT_HIDE_GRID_LINES)
        self._rowGroupControlAfter    = gridProps.get('rowGroupControlAfter', DEFAULT_ROW_GROUP_CONTROL_AFTER)
        self._columnGroupControlAfter = gridProps.get('columnGroupControlAfter', DEFAULT_COLUMN_GROUP_CONTROL_AFTER)


    def _refreshData(self):
        # Get all the sheet data:
        response = SERVICE.spreadsheets().values().get(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!A1:%s%s' % (self._title, getColumnLetterOf(self._columnCount), self._rowCount)).execute(); _logReadRequests()

        sheetData = response.get('values', [[]])
        self._cells = {}
        if response['majorDimension'] == 'ROWS':
            for rowNumBase0, row in enumerate(sheetData):
                for colNumBase0, sheetDatum in enumerate(row):
                    self._cells[(colNumBase0 + 1, rowNumBase0 + 1)] = sheetDatum
        elif response['majorDimension'] == 'COLUMNS':
            for colNumBase0, column in enumerate(sheetData):
                for rowNumBase0, sheetDatum in enumerate(column):
                    self._cells[(colNumBase0 + 1, rowNumBase0 + 1)] = sheetDatum


    def _updateGridProperties(self):
        gridProperties = {'rowCount':                self._rowCount,
                          'columnCount':             self._columnCount,
                          'frozenRowCount':          self._frozenRowCount,
                          'frozenColumnCount':       self._frozenColumnCount,
                          'hideGridlines':           self._hideGridlines,
                          'rowGroupControlAfter':    self._rowGroupControlAfter,
                          'columnGroupControlAfter': self._columnGroupControlAfter}
        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet._spreadsheetId,
            body={
            'requests': [{'updateSheetProperties': {'properties': {'sheetId': self._sheetId,
                                                                   'gridProperties': gridProperties},
                                                    'fields': 'gridProperties'}}]})
        request.execute(); _logWriteRequest()


    def _enlargeIfNeeded(self, requestedColumn=None, requestedRow=None):
        # Increase rowCount or columnCount if needed.
        if requestedColumn is None:
            requestedColumn = self._columnCount
        if requestedRow is None:
            requestedRow = self._rowCount

        # Enlarge the sheet:
        self.resize(max(requestedColumn, self._columnCount),
                    max(requestedRow, self._rowCount))


    def update(self, *args):
        if len(args) == 3: # args are column, row like (2, 5)
            column, row, value = args
        elif len(args) == 2: # args is a string of a grid cell like ('B5',)
            if isinstance(args[0], int) and isinstance(args[1], int):
                raise TypeError('You most likely have forgotten to supply a value to update the this cell with.')
            column, row = convertToColumnRowInts(args[0])
            value = args[1]
        else:
            raise TypeError("get() takes one or two arguments, like ('A1',) or (2, 5)")

        if not isinstance(column, int):
            raise TypeError('column indices must be integers, not %s' % (type(column).__name__))
        if not isinstance(row, int):
            raise TypeError('row indices must be integers, not %s' % (type(row).__name__))
        if column < 1 or row < 1:
            raise IndexError('Column %s, row %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index. Negative indices are not supported by ezsheets.' % (column, row))

        self._enlargeIfNeeded(column, row)

        cellLocation = getColumnLetterOf(column) + str(row)
        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!%s:%s' % (self._title, cellLocation, cellLocation),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'ROWS',
                'values': [[value]],
                #'range': '%s!%s:%s' % (self._title, cellLocation, cellLocation),
                }
            )
        request.execute(); _logWriteRequest()

        self._cells[(column, row)] = value



    def updateRow(self, row, values):
        if not isinstance(row, int):
            raise TypeError('row indices must be integers, not %s' % (type(row).__name__))
        if row < 1:
            raise IndexError('Row %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index.' % (row))
        if not isinstance(values, (list, tuple)):
            raise TypeError('values must be a list or tuple, not %s' % (type(values).__name__))

        if isinstance(values, tuple):
            values = list(values)
        if len(values) < self._columnCount:
            values.extend([''] * (self._columnCount - len(values)))

        self._enlargeIfNeeded(None, row)

        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!A%s:%s%s' % (self._title, row, getColumnLetterOf(len(values)), row),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'ROWS',
                'values': [values],
                #'range': '%s!A%s:%s%s' % (self._title, row, getColumnLetterOf(len(values)), row),
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        for colNumBase1 in range(1, self._columnCount+1):
            self._cells[(colNumBase1, row)] = values[colNumBase1-1]


    def updateColumn(self, column, values):
        if not isinstance(column, (int, str)):
            raise TypeError('column indices must be integers, not %s' % (type(column).__name__))
        if isinstance(column, int) and column < 1:
            raise IndexError('Column %s does not exist. Google Sheets\' columns and rows are 1-based, not 0-based. Use index 1 instead of index 0 for row and column index.' % (column))
        if not isinstance(values, (list, tuple)):
            raise TypeError('values must be a list or tuple, not %s' % (type(values).__name__))
        if isinstance(column, str) and not column.isalpha():
            raise ValueError('Column %s does not exist. Columns must be a 1-based int or a letters-only str.')

        if isinstance(values, tuple):
            values = list(values)
        if isinstance(column, str):
            column = getColumnNumber(column)

        if len(values) < self._rowCount:
            values.extend([''] * (self._rowCount - len(values)))

        self._enlargeIfNeeded(column, None)

        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!%s1:%s%s' % (self._title, getColumnLetterOf(column), getColumnLetterOf(column), len(values)),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'COLUMNS',
                'values': [values],
                #'range': '%s!%s1:%s%s' % (self._title, getColumnLetterOf(column), getColumnLetterOf(column), len(values)),
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        for rowNumBase1 in range(1, self._rowCount+1):
            self._cells[(column, rowNumBase1)] = values[rowNumBase1-1]


    def updateRows(self, rows, startRow=1):
        # Argument validation:
        # Ensure that `rows` is a list of lists:
        if not isinstance(rows, (list, tuple)):
            raise TypeError('rows arg must be a list/tuple of lists/tuples, not %s' % (type(rows).__name__))
        for row in rows:
            if not isinstance(row, (list, tuple)):
                raise TypeError('rows arg contains a non-list/tuple')

        if not isinstance(startRow, int):
            raise TypeError('startRow arg must be an int, not %s' % (type(startRow).__name__))
        if startRow < 1:
            raise ValueError('startRow arg is 1-based, and must be 1 or greater, not %r' % (startRow))

        if startRow > self._rowCount:
            return # No rows to update, so return.

        # Find out the max length of a row in `rows`. This will be the new columnCount for the sheet:
        maxColumnCount = self._columnCount
        for row in rows:
            maxColumnCount = max(maxColumnCount, len(row))

        # Lengthen rows to the length of self._rowCount, and each row to the length of self._columnCount:
        for row in rows:
            row.extend([''] * (maxColumnCount - len(row))) # pad each row
        while len(rows) < (self._rowCount - startRow + 1): # TODO - this could probably be made more performant if we use extend().
            rows.append([''] * self._columnCount) # pad extra rows

        self._enlargeIfNeeded(None, len(rows) + startRow - 1)

        # Send the API request that updates the Google sheet.
        #rangeCells = '%s!A%s:%s%s' % (self._title, startRow, getColumnLetterOf(maxColumnCount), stopRow - 1)
        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!A%s:%s%s' % (self._title, startRow, getColumnLetterOf(maxColumnCount), startRow + len(rows) - 1),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'ROWS',
                'values': rows,
                #'range': rangeCells,
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        for rowNumBase1 in range(startRow, startRow + len(rows)):
            for colNumBase0 in range(maxColumnCount):
                self._cells[(colNumBase0+1, rowNumBase1)] = rows[rowNumBase1-startRow][colNumBase0]

    def updateColumns(self, columns, startColumn=1):
        # Argument validation:
        # Ensure that `columns` is a list of lists:
        if not isinstance(columns, (list, tuple)):
            raise TypeError('columns arg must be a list/tuple of lists/tuples, not %s' % (type(columns).__name__))
        for column in columns:
            if not isinstance(column, (list, tuple)):
                raise TypeError('columns arg contains a non-list/tuple')

        if not isinstance(startColumn, int):
            raise TypeError('startColumn arg must be an int, not %s' % (type(startColumn).__name__))
        if startColumn < 1:
            raise ValueError('startColumn arg is 1-based, and must be 1 or greater, not %r' % (startColumn))

        if startColumn > self._columnCount:
            return # No rows to update, so return.

        # Find out the max length of a column in `columns`. This will be the new rowCount for the sheet:
        maxRowCount = self._rowCount
        for column in columns:
            maxRowCount = max(maxRowCount, len(column))

        # Lengthen columns to the length of self._columnCount, and each column to the length of self._rowCount:
        for column in columns:
            column.extend([''] * (maxRowCount - len(column))) # pad each column
        while len(columns) < (self._columnCount - startColumn + 1): # TODO - this could probably be made more performant if we use extend().
            columns.append([''] * self._rowCount) # pad extra columns

        self._enlargeIfNeeded(len(columns) + startColumn - 1, None)

        # Send the API request that updates the Google sheet.
        #rangeCells = '%s!A%s:%s%s' % (self._title, startRow, getColumnLetterOf(maxColumnCount), stopRow - 1)
        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!%s1:%s%s' % (self._title, getColumnLetterOf(startColumn), getColumnLetterOf(startColumn + len(columns) - 1), maxRowCount),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'COLUMNS',
                'values': columns,
                #'range': rangeCells,
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        for colNumBase1 in range(startColumn, startColumn + len(columns)):
            for rowNumBase0 in range(maxRowCount):
                self._cells[(colNumBase1, rowNumBase0+1)] = columns[colNumBase1-startColumn][rowNumBase0]

    """
    def updateColumns(self, columns, startColumn=0, stopColumn=None, step=1):
        # Ensure that `columns` is a list of lists:
        if not isinstance(columns, (list, tuple)):
            raise TypeError('columns arg must be a list/tuple of lists/tuples, not %s' % (type(columns).__name__))
        for value in columns:
            if not isinstance(columns, (list, tuple)):
                raise TypeError('columns arg must be a list/tuple of lists/tuples, not %s' % (type(columns).__name__))

        if stopColumn is None:
            stopColumn = self._columnCount + 1

        # Lengthen columns to the length of self._columnCount, and each column to the length of self._rowCount:
        for column in columns:
            column.extend([''] * (self._columnCount - len(column))) # pad each column
        if len(columns) < self._columnCount:
            columns.extend([[''] * self._columnCount for i in range(self.stopColumn - len(columns) - 1)]) # pad extra columns

        self._enlargeIfNeeded(len(columns) + startColumn - 1, len(columns[0]))

        # Send the API request that updates the Google sheet.
        rangeCells = '%s!%s1:%s%s' % (self._title, getColumnLetterOf(startColumn), getColumnLetterOf(len(columns)), len(columns[0]))
        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range=rangeCells,
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'COLUMNS',
                'values': columns,
                #'range': rangeCells,
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        for colNumBase0 in range(len(columns)):
            for rowNumBase0 in range(len(columns[0])):
                self._cells[(colNumBase0+1, rowNumBase0+1)] = columns[colNumBase0][rowNumBase0]
    """

    def clear(self):
        request = SERVICE.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet._spreadsheetId,
            range='%s!A1:%s%s' % (self._title, getColumnLetterOf(self._columnCount), self._rowCount),
            valueInputOption='USER_ENTERED', # Details at https://developers.google.com/sheets/api/reference/rest/v4/ValueInputOption
            body={
                'majorDimension': 'ROWS',
                'values': [[''] * self._columnCount for i in range(self._rowCount)],
                #'range': rangeCells,
                }
            )
        request.execute(); _logWriteRequest()

        # Update the local data in `_cells`:
        self._cells = {}


    def copyTo(self, destinationSpreadsheetId):
        request = SERVICE.spreadsheets().sheets().copyTo(spreadsheetId=self._spreadsheet._spreadsheetId,
                                                         sheetId=self._sheetId,
                                                         body={'destinationSpreadsheetId': destinationSpreadsheetId})
        request.execute(); _logWriteRequest()


    def delete(self):
        if len(self._spreadsheet.sheets) == 1:
            raise ValueError('Cannot delete all sheets; spreadsheets must have at least one sheet')

        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet._spreadsheetId,
            body={
                'requests': [{'deleteSheet': {'sheetId': self._sheetId}}]})
        request.execute(); _logWriteRequest()
        self._spreadsheet.refresh() # Refresh the spreadsheet's list of sheets.


    def resize(self, columnCount=None, rowCount=None):
        # NOTE: If you try to specify the rowCount without the columnCount
        # (and vice versa), Google Sheets thinks you want to set the
        # columnCount to 0 and then complains that you can't delete all the
        # columns.
        # We have a resize() method so that the user doesn't set the row/column
        # count back to the local setting in this Sheet object when it has
        # been changed on Google Sheets by another user. The rowCount and
        # columnCount property setters will make a request to get the current
        # sizes so they don't mistakenly change the other dimension, but
        # this won't be an atomic operation like resize() is.

        # As of Feb 2019, Google Sheets has a cell max of 5,000,000, but
        # this could change so ezsheets won't catch it.

        # Google Sheets size limits are documented here:
        #   https://support.google.com/drive/answer/37603?hl=en
        #   https://www.quora.com/What-are-the-limits-of-Google-Sheets
        if rowCount is None and columnCount is None:
            return # No resizing is taking place, so this function is a no-op.
        if rowCount == self._rowCount and columnCount == self._columnCount:
            return # No change needed, so just return.

        # A None value means "use the current setting"
        if rowCount is None:
            rowCount = self._rowCount
        if columnCount is None:
            columnCount = self._columnCount

        if isinstance(columnCount, str):
            columnCount = getColumnNumber(columnCount)

        if not isinstance(rowCount, int):
            raise TypeError('rowCount arg must be an int, not %s' % (type(rowCount).__name__))
        if not isinstance(columnCount, int):
            raise TypeError('columnCount arg must be an int, not %s' % (type(columnCount).__name__))

        if rowCount < 1:
            raise TypeError('rowCount arg must be a positive nonzero int, not %r' % (rowCount))
        if columnCount < 1:
            raise TypeError('columnCount arg must be a positive nonzero int, not %r' % (columnCount))


        request = SERVICE.spreadsheets().batchUpdate(spreadsheetId=self._spreadsheet._spreadsheetId,
        body={
            'requests': [{'updateSheetProperties': {'properties': {'sheetId': self._sheetId,
                                                                   'gridProperties': {'rowCount': rowCount,
                                                                                      'columnCount': columnCount}},
                                                    'fields': 'gridProperties'}}]})
        request.execute(); _logWriteRequest()
        self._rowCount = rowCount
        self._columnCount = columnCount

    def __iter__(self):
        return iter(self.getRows())

    def downloadAsCSV(self):
        pass # TODO
    def downloadAsExcel(self):
        pass # TODO
    def downloadAsODS(self):
        pass # TODO
    def downloadAsPDF(self):
        pass # TODO
    def downloadAsHTML(self):
        pass # TODO
    def downloadAsTSV(self):
        pass # TODO


def _getTabColorArg(value):
    if isinstance(value, str) and value in COLORS:
        # value is a color string from colorvalues.py, like 'red' or 'black'
        tabColorArg = {
            'red':   COLORS[value][0],
            'green': COLORS[value][1],
            'blue':  COLORS[value][2],
            'alpha': COLORS[value][3],
        }

    #elif value is None: # TODO - apparently there's no way to reset the color through the api?
    #    tabColorArg = {} # Reset the color
    elif isinstance(value, (list, tuple)) and len(value) in (3, 4):
        # value is a tuple of three or four floats (ranged from 0.0 to 1.0)
        tabColorArg = {
            'red': float(value[0]),
            'green': float(value[1]),
            'blue': float(value[2]),
        }
        try:
            tabColorArg['alpha'] = value[3]
        except:
            tabColorArg['alpha'] = 1.0
    elif value is None:
        return None # Represents no tabColor setting.
    elif type(value) == dict:
        tabColorArg = value
    else:
        raise ValueError("value argument must be a color string like 'red', a 3- or 4-float tuple for an RGB or RGBA value, or a dict")

    # Set any remaining unspecified defaults.
    tabColorArg.setdefault('red', 0.0)
    tabColorArg.setdefault('green', 0.0)
    tabColorArg.setdefault('blue', 0.0)
    tabColorArg.setdefault('alpha', 1.0)
    tabColorArg['red']   = float(tabColorArg['red'])
    tabColorArg['green'] = float(tabColorArg['green'])
    tabColorArg['blue']  = float(tabColorArg['blue'])
    tabColorArg['alpha'] = float(tabColorArg['alpha'])
    return tabColorArg


def convertToColumnRowInts(arg):
    if not isinstance(arg, str):
        raise TypeError("argument must be a grid cell str, like 'A1', not of type %s" % (type(arg).__name__))
    if not arg.isalnum() or not arg[0].isalpha() or not arg[-1].isdecimal():
        raise ValueError("argument must be a grid cell str, like 'A1', not %r" % (arg))

    for i in range(1, len(arg)):
        if arg[i].isdecimal():
            column = getColumnNumber(arg[:i])
            row = int(arg[i:])
            return (column, row)

    assert False # pragma: no cover We know this will always return before this point because arg[-1].isdecimal().


def createSpreadsheet(title=''):
    if not IS_INITIALIZED: init() # Initialize this module if not done so already.
    request = SERVICE.spreadsheets().create(body={
        'properties': {'title': title}
        })
    response = request.execute(); _logWriteRequest()

    return Spreadsheet(response['spreadsheetId'])


def getIdFromUrl(url):
    # https://docs.google.com/spreadsheets/d/16RWH9XBBwd8pRYZDSo9EontzdVPqxdGnwM5MnP6T48c/edit#gid=0
    if url.startswith('https://docs.google.com/spreadsheets/d/'):
        spreadsheetId = url[39:url.find('/', 39)]
    else:
        spreadsheetId = url

    if re.match('^([a-zA-Z0-9]|_|-)+$', spreadsheetId) is None:
        raise ValueError('url argument must be an alphanumeric id or a full URL')
    return spreadsheetId


def getColumnLetterOf(columnNumber):
    """getColumnLetterOf(1) => 'A', getColumnLetterOf(27) => 'AA'"""
    if not isinstance(columnNumber, int):
        raise TypeError('columnNumber must be an int, not a %r' % (type(columnNumber).__name__))
    if columnNumber < 1:
        raise ValueError('columnNumber must be an int value of at least 1')

    letters = []
    while columnNumber > 0:
        columnNumber, remainder = divmod(columnNumber, 26)
        if remainder == 0:
            remainder = 26
            columnNumber -= 1
        letters.append(chr(remainder + 64))
    return ''.join(reversed(letters))


def getColumnNumber(columnLetter):
    """getColumnNumber('A') => 1, getColumnNumber('AA') => 27"""
    if not isinstance(columnLetter, str):
        raise TypeError('columnLetter must be a str, not a %r' % (type(columnLetter).__name__))
    if not columnLetter.isalpha():
        raise ValueError('columnLetter must be composed of only letters')

    columnLetter = columnLetter.upper()
    digits = []

    while columnLetter:
        digits.append(ord(columnLetter[0]) - 64)
        columnLetter = columnLetter[1:]

    number = 0
    place = 0
    for digit in reversed(digits):
        number += digit * (26 ** place)
        place += 1

    return number


def init(credentialsFile='credentials.json', tokenFile='token.pickle'):
    global SERVICE, IS_INITIALIZED

    if not os.path.exists(credentialsFile):
        raise EZSheetsException('Can\'t find credentials file at %s. You can download this file from https://developers.google.com/gmail/api/quickstart/python and clicking "Enable the Gmail API"' % (os.path.abspath(credentialsFile)))

    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server()
        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    SERVICE = build('sheets', 'v4', credentials=creds)
    IS_INITIALIZED = True




init()
s = Spreadsheet('https://docs.google.com/spreadsheets/d/1GfFDkD7LfwlVSLQMVQILaz2BPARG7Ott5Ui-frh0m2Y/edit#gid=0')
# m = Spreadsheet('https://docs.google.com/spreadsheets/d/10tRbpHZYkfRecHyRHRjBLdQYoq5QWNBqZmH9tt4Tjng/edit#gid=0') # (My spreadsheet since I don't have access to yours)