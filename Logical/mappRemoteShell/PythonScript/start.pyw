# ----------------------------------------------------------------------------------------
# import libraries and functions
import subprocess
import platform
import sys
import time
import os
import configparser
from uaclient import UaClient
from asyncua.sync import ua
from timeloop import Timeloop
from datetime import timedelta
from datetime import datetime
from window import Ui_MainWindow
from PyQt5 import QtCore, QtGui, QtWidgets

frmMain = None
config_path = None
config = None

# ----------------------------------------------------------------------------------------
# fix windows taskbar icon
if platform.system() == "Windows":
    import ctypes
    myappid = u'B&R.mappRemoteShell.V1_0'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

# ----------------------------------------------------------------------------------------
# fix high resolution scaling
if hasattr(QtCore.Qt, 'AA_EnableHighDpiScaling'):
    QtWidgets.QApplication.setAttribute(
        QtCore.Qt.AA_EnableHighDpiScaling, True)
if hasattr(QtCore.Qt, 'AA_UseHighDpiPixmaps'):
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

# ----------------------------------------------------------------------------------------
# local constants
PING_INTERVAL = 3
LOGGER_MAX_LEN = 50000
RESPONSE_TIMEOUT = 2
RESPONSE_STRING_SIZE = 2000
ERR_COMMAND_EXECUTE = 10000
ERR_COMMAND_NOT_FOUND = 10001
ERR_RESPONSE_SIZE = 10002
ERR_USER_ACCESS = 10003

# ----------------------------------------------------------------------------------------
# try to ping opc server
def ping_ip(current_ip_address:str):
    try:
        str_ping = "ping -{} 1 {}".format('n' if platform.system().lower() == "windows" else 'c', current_ip_address)
        result = subprocess.Popen(str_ping, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).wait()
        if result:
            return False
        else:
            return True
    except Exception as e:
        print(e)
        return False

# ----------------------------------------------------------------------------------------
# start cyclic timer for ping
tl = Timeloop()
@tl.job(interval=timedelta(seconds=PING_INTERVAL))
def ping_job():
    if frmMain != None:
        if ping_ip(frmMain.txtPLC_IP.text()):
            # show ping status ok
            frmMain.labPLC_Ping_Status.setStyleSheet("color: green;")
            frmMain.labPLC_Ping_Status.setText("OK")
            frmMain.btnConnect.setEnabled(True)
            frmMain.can_ping = True
        else:
            # show ping status not ok
            frmMain.labPLC_Ping_Status.setStyleSheet("color: red;")
            frmMain.labPLC_Ping_Status.setText("Failed")
            frmMain.btnConnect.setEnabled(False)
            frmMain.can_ping = False

# ----------------------------------------------------------------------------------------
# callback for opc ua value change
class DataChangeHandler(QtCore.QObject):
    data_change = QtCore.pyqtSignal(object, int)

    def __init__(self):
        super(DataChangeHandler, self).__init__()

    # new comand request
    def datachange_notification(self, node, val, data):
        self.data_change.emit(node, val)

class OpcAliveCounter(QtCore.QThread):
    # external signals
    sig_log = QtCore.pyqtSignal(str, bool)
    reconnect = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QThread.__init__(self)
        self.alive_counter = 0

    def __del__(self):
        self.wait()

    # make sure PLC is still conected
    @tl.job(interval=timedelta(seconds=PING_INTERVAL))
    def alive_timer():
        ## sometimes the timer is faster than the ui
        if frmMain != None:
            # only count when connection is active
            if frmMain.threadOPC != None and frmMain.threadOPC.client != None and frmMain.threadOPC.client._connected:
                # count alive counter up
                frmMain.alive_counter = frmMain.alive_counter + 1
            # connection expired after 3x ping counter
            if frmMain.alive_counter > 3:
                # report disconnect
                frmMain.threadAliveCounter.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " connection was interrupted", True)
                time.sleep(0.1)
                # close connection, ignore errors
                try:
                    frmMain.threadOPC.disconnect()
                except Exception as e:
                    print(e)
                # reset counter, display connect button
                frmMain.alive_counter = 0
                frmMain.btnConnect.setVisible(True)
                return

            # reconnect to OPC UA server
            IsConnectd = frmMain.threadOPC != None and frmMain.threadOPC.client != None and frmMain.threadOPC.client._connected 
            if not IsConnectd and frmMain.threadAliveCounter != None and frmMain.chkReconnect.isChecked() and frmMain.can_ping:
                # tell main thread to reconnect
                frmMain.threadAliveCounter.reconnect.emit()

    # ----------------------------------------------------------------------------------------
    # connect to OPC UA server
    def run(self):
        self.alive_counter = 0
     
# ----------------------------------------------------------------------------------------
# OPC UA client class
class OpcClientThread(QtCore.QThread):
    # external signals
    sig_log = QtCore.pyqtSignal(str, bool)

    def __init__(self):
        QtCore.QThread.__init__(self)
        self.client = None
        self.result = None

    def __del__(self):
        self.wait()

    # new data from callback function
    def data_changed(self, node, val):
        nodeStatus = None
        frmMain.alive_counter = 0
        # exceute new command
        if "execute" in str(node) and val:
            try:
                # get command variable
                nodeCommand = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".command")
                valCommand = nodeCommand.get_value()
                # get command variable
                nodeResponse = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".response")
                # set status variable to 65535
                nodeStatus = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".status")
                dv = ua.DataValue(ua.Variant([65535], ua.VariantType.UInt16))
                nodeStatus.set_value(dv)
                # execute command
                self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " new command -> " + valCommand, True)
                result = subprocess.Popen(valCommand, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)

                # check if we have response data, ignore errors
                try:
                    str_response = ""
                    stdout_value = ""
                    stdout_value, stderr_value = result.communicate(timeout=RESPONSE_TIMEOUT)
                    if stdout_value != "":
                        stdout_value = stdout_value.decode('utf8', errors='backslashreplace').replace('\r', '')
                        self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " command response -> " + stdout_value, False)
                        # make sure response data fits into response string
                        if len(stdout_value) <= RESPONSE_STRING_SIZE:
                            str_response = stdout_value
                        else:
                            str_response = stdout_value[0:RESPONSE_STRING_SIZE]

                except Exception as e:
                    print(e)

                # send response data
                dv = ua.DataValue(ua.Variant([str_response], ua.VariantType.String))
                nodeResponse.set_value(dv)
                if len(stdout_value) > RESPONSE_STRING_SIZE:
                    self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " response error -> maximum string size ("+ str(RESPONSE_STRING_SIZE) + ") exceeded limit (" + str(len(stdout_value)) + ")", True)
                    dv = ua.DataValue(ua.Variant([ERR_RESPONSE_SIZE], ua.VariantType.UInt16))
                else:
                    self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " command successful", False)
                    dv = ua.DataValue(ua.Variant([0], ua.VariantType.UInt16))

                # set status variable
                nodeStatus.set_value(dv)
                time.sleep(0.5)

            except Exception as e:
                print(e)

                self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " command error -> " + str(e), True)
                # set status variable to 1
                if nodeStatus != None:
                    if hasattr(e, 'code') and e.code == 2149515264:
                        # set status for command not found
                        self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " No permission to write to OPC UA variable", True)
                        self.disconnect()
                        return
                    elif hasattr(e, 'errno') and e.errno == 2:
                        # set status for command not found
                        dv = ua.DataValue(ua.Variant([ERR_COMMAND_NOT_FOUND], ua.VariantType.UInt16))
                        nodeStatus.set_value(dv)
                    else:
                        # set status for generic command failed
                        dv = ua.DataValue(ua.Variant([ERR_COMMAND_EXECUTE], ua.VariantType.UInt16))
                        nodeStatus.set_value(dv)

                    # send response data
                    dv = ua.DataValue(ua.Variant(["Command error -> " + str(e)], ua.VariantType.String))
                    nodeResponse.set_value(dv)

            finally:
                try:
                    # reset execute variable on PLC
                    if frmMain.threadOPC.client._connected:
                        exc_opc = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".execute")
                        dv = ua.DataValue(ua.Variant([False], ua.VariantType.Boolean))
                        exc_opc.set_value(dv)
                except Exception as e:                    
                    print(e)

        try:
        # reset alive counter on PLC
            if "alive_counter" in str(node) and val > 500 and frmMain.threadOPC.client._connected:
                dv = ua.DataValue(ua.Variant([0], ua.VariantType.UInt16))
                node.set_value(dv)
        except Exception as e:                    
            print(e)

    # ----------------------------------------------------------------------------------------
    # connect to OPC UA server
    def run(self):
        frmMain.alive_counter = 0

    # ----------------------------------------------------------------------------------------
    # find pv namespace
    def find_namespace(self, ns_target):
        ns_node = self.client.get_node(ua.NodeId(ua.ObjectIds.Server_NamespaceArray))
        namespaces = ns_node.get_value()
        # Find index of 'urn:B&R/pv/'
        ns_index = None
        for idx, ns in enumerate(namespaces):
            if ns == ns_target:
                ns_index = idx
                break

        return ns_index      
        
    # ----------------------------------------------------------------------------------------
    # connect to OPC UA server
    def connect(self, server_url):
        try:
            # create client connection
            self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " connecting...", False)
            self.client = UaClient()
            self.client.connect("opc.tcp://" + server_url)
            time.sleep(0.5)
            self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " connected successful", True)

            # Find pv namespace
            self.ns_index = self.find_namespace('http://br-automation.com/OpcUa/PLC/PV/')
            if self.ns_index is not None:
                self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + f" Namespace found at index {self.ns_index}", True)
            else:
                self.ns_index = self.find_namespace('urn:B&R/pv/')
                if self.ns_index is not None:
                    self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + f" Namespace found at index {self.ns_index}", True)
                else:
                    self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " Namespace not found", True)
                    # close connection, ignore errors
                    try:
                        self.btnConnect.setVisible(False)
                        self.disconnect()
                    except Exception as e:
                        print(e)
                    return

            # check if task exists on PLC
            var_structure = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task)
            result = var_structure.get_children()
            if len(result) != 0:
                self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " support task " + config_plc_task + " found on PLC", False)
                result = var_structure.get_children()
                opc_var_found = False

                for opc_var in result:
                    print(opc_var)

                    if str(opc_var) == "ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var:
                        self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " variable " + config_plc_var + "structure found on PLC", False)
    
                        # connect opc variables
                        varExecute = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".execute")
                        varAliveCounter = self.client.get_node("ns=" + str(self.ns_index) + ";s=::" + config_plc_task + ":" + config_plc_var + ".alive_counter")
                        subHandler = DataChangeHandler()
                        subHandler.data_change.connect(self.data_changed, type=QtCore.Qt.QueuedConnection)
                        # create subscription
                        self.client.subscribe_datachange(varExecute, subHandler)
                        self.client.subscribe_datachange(varAliveCounter, subHandler)
                        # exit for
                        opc_var_found = True
                        break

                if not opc_var_found:
                    self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " variable structure " + config_plc_var + " is missing on PLC or is not activated for OPC UA access", False)
                    # close connection, ignore errors
                    try:
                        self.btnConnect.setVisible(False)
                        self.disconnect()
                    except Exception as e:
                        print(e)
            else:
                self.sig_log.emit(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " task " + config_plc_task + " or variable " + config_plc_var + " is missing or variable " + config_plc_var + " is not activated for OPC UA access , make sure mappRemote task is running on PLC", True)
                # close connection, ignore errors
                try:
                    self.btnConnect.setVisible(False)
                    self.disconnect()
                except Exception as e:
                    print(e)

        except Exception as e:
            raise e

    # ----------------------------------------------------------------------------------------
    # disconnect from OPC UA server
    def disconnect(self):
        # disconnect, ignore errors
        try:
            self.client.disconnect()
        except Exception as e:                    
            print(e)
            
# ----------------------------------------------------------------------------------------
# PyQt main frame class
class MappRemoteShell(QtWidgets.QMainWindow, Ui_MainWindow):
    # local constants
    BadNodeIdUnknown = 2150891520
    BadNodeIdInvalid = 2150825984
    BadWriteNotSupported = 2155020288
    ConnectionRefusedError = 10061
    ConnectionTimedOut = "timed out"

    # ----------------------------------------------------------------------------------------
    # initialize application
    def __init__(self, ip, port_opcua, show_balloon, show_reconnect, show_minimized):
        super(MappRemoteShell, self).__init__()
        self.setupUi(self)
        self.threadOPC = None
        self.setWindowIcon(QtGui.QIcon("mapp.png"))
        # start connection alive cyclic task
        self.threadAliveCounter = OpcAliveCounter()
        self.threadAliveCounter.start()
        self.threadAliveCounter.sig_log.connect(self.add_log)
        self.threadAliveCounter.reconnect.connect(self.connect_opcua)
        self.alive_counter = 0
        self.can_ping = False
        # add button connect and exit event
        self.btnConnect.clicked.connect(self.connect_opcua)
        self.btnExit.clicked.connect(self.exit_app)
        # add config events
        self.txtPLC_IP.setText(ip)
        self.txtPLC_IP.textChanged.connect(self.config_ip)
        self.txtPLC_Port.setText(port_opcua)
        self.txtPLC_Port.textChanged.connect(self.config_port_opcua)
        self.chkBalloon.setChecked(show_balloon)
        self.chkBalloon.clicked.connect(self.config_balloon)
        self.chkBalloon.setChecked(show_balloon)
        self.chkReconnect.clicked.connect(self.config_reconnect)
        self.chkReconnect.setChecked(show_reconnect)
        self.chkMinimized.clicked.connect(self.config_minimized)
        self.chkMinimized.setChecked(show_minimized)

    # ip changed event
    def config_ip(self):
        global config, config_path
        config.set('eth', 'ip', self.txtPLC_IP.text())
        with open(config_path, 'w') as configfile:
            config.write(configfile)

    def config_port_opcua(self):
        global config, config_path
        config.set('eth', 'port_opcua', self.txtPLC_Port.text())
        with open(config_path, 'w') as configfile:
            config.write(configfile)            

    # checkbox balloon event
    def config_balloon(self):
        global config, config_path
        if self.chkBalloon.isChecked():
            config.set('default', 'show_balloon', "True")
        else:
            config.set('default', 'show_balloon', "False")
        with open(config_path, 'w') as configfile:
            config.write(configfile)

    # checkbox reconnect event
    def config_reconnect(self):
        global config, config_path
        if self.chkReconnect.isChecked():
            config.set('default', 'auto_reconnect', "True")
        else:
            config.set('default', 'auto_reconnect', "False")
        with open(config_path, 'w') as configfile:
            config.write(configfile)

    # checkbox minimized event
    def config_minimized(self):
        global config, config_path
        if self.chkMinimized.isChecked():
            config.set('default', 'start_minimized', "True")
        else:
            config.set('default', 'start_minimized', "False")
        with open(config_path, 'w') as configfile:
            config.write(configfile)            

    # button connect event
    def connect_opcua(self):
        # create OPC UA client thread on first call
        if self.threadOPC == None:
            self.threadOPC = OpcClientThread()
            self.threadOPC.start()
            self.threadOPC.sig_log.connect(self.add_log)
        # connect to OPC UA server
        try:
            self.threadOPC.connect(frmMain.txtPLC_IP.text() + ":" + frmMain.txtPLC_Port.text())
            self.btnConnect.setVisible(False)
        except Exception as e:
            print(e)

            if len(e.args) > 0:
                if e.args[0] == self.ConnectionRefusedError:
                    self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " connection refused, make sure IP address and port is correct and OPC server is running", True)
                elif e.args[0] == self.ConnectionTimedOut:
                    self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " timed out, make sure IP address and port is correct and OPC server is running", True)
                elif e.args[0] == self.BadNodeIdUnknown or e.args[0] == self.BadNodeIdInvalid:
                    self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " task " + config_plc_task + " or variable " + config_plc_var + " is missing or variable " + config_plc_var + " is not activated for OPC UA access , make sure mappRemote task is running on PLC", True)
                elif e.args[0] == self.BadWriteNotSupported:
                    self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " mappRemote variable no write access", True)
                else:
                    self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " unexpected error:" + str(sys.exc_info()[0]), True)
            else:
                self.add_log(str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")) + " unexpected error:" + str(sys.exc_info()[0]), True)

    # ignore application close with X and send app to tray
    def closeEvent(self, event):
        self.showMinimized()
        event.ignore()

    # button exit app
    def exit_app(self):
        if self.threadOPC != None:
            # close connection
            try:
                # disconnect and delete all threads
                self.threadOPC.disconnect()
                self.threadOPC.quit()
                self.threadOPC.wait()
                self.threadAliveCounter.quit()
                self.threadAliveCounter.wait()
                self.threadOPC.deleteLater()
                self.threadAliveCounter.deleteLater()
            except Exception as e:
                print(e)
        # finally quit application
        app.quit()
        sys.exit(app.exec_())

    # new log entry from client thread
    def add_log(self, post_text, balloon):
        new_text = self.txtStatus.toPlainText() + post_text + "\r"
        if len(new_text) > LOGGER_MAX_LEN:
            new_text = new_text[len(new_text)-LOGGER_MAX_LEN: len(new_text)]
        self.txtStatus.setPlainText(new_text)
        self.txtStatus.moveCursor(QtGui.QTextCursor.End)
        # show balloon text        
        if balloon and self.chkBalloon.isChecked():
            trayIcon.showMessage("mappRemoteShell", post_text, QtWidgets.QSystemTrayIcon.Information)

# ----------------------------------------------------------------------------------------
# system tray event show form
def show_form():
    frmMain.showMaximized()
# system tray event exit app
def exit_app():
    if frmMain.threadOPC != None:
        frmMain.threadOPC.deleteLater()
    app.quit()
    sys.exit(app.exec_())

# ----------------------------------------------------------------------------------------
# start ui and ping timer
if __name__ == '__main__':
    config = configparser.ConfigParser()
    
    # Get the directory where the script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.ini")
    
    # Check if config.ini exists, create with defaults if not
    if not os.path.exists(config_path):
        config['eth'] = {
            'ip': '127.0.0.1',
            'port_opcua': '4840'
        }
        config['plc'] = {
            'task': 'mpRemote',
            'var': 'mappRemoteShell'
        }
        config['default'] = {
            'show_balloon': 'False',
            'auto_reconnect': 'False',
            'start_minimized': 'False'
        }
        with open(config_path, 'w') as configfile:
            config.write(configfile)
    
    config.read(config_path)
    config_ip = config.get('eth', 'ip')
    config_port_opcua = config.get('eth', 'port_opcua')
    config_plc_task = config.get('plc', 'task')
    config_plc_var = config.get('plc', 'var')
    config_balloon = config.get('default', 'show_balloon') == "True"
    config_reconnect = config.get('default', 'auto_reconnect') == "True"
    config_minimized = config.get('default', 'start_minimized') == "True"
    # start ping timer
    tl.start(block=False)
    # create application
    app = QtWidgets.QApplication(sys.argv)
    # create system tray support
    trayIcon = QtWidgets.QSystemTrayIcon(QtGui.QIcon("mapp.png"))
    trayIcon.setToolTip('Open mappRemoteShell window')
    trayIcon.show()
    menu = QtWidgets.QMenu()
    showAction = menu.addAction('Show')
    showAction.triggered.connect(show_form)
    exitAction = menu.addAction('Exit')
    exitAction.triggered.connect(exit_app)
    trayIcon.setContextMenu(menu)
    trayIcon.activated.connect(show_form)
    # create main form
    frmMain = MappRemoteShell(config_ip, config_port_opcua,config_balloon, config_reconnect, config_minimized)
    if not config_minimized:
        frmMain.show()
    app.exec_()

    sys.exit(app.exec_())
