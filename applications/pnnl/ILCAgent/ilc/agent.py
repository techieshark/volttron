import re
import sys
import logging
import datetime
# import pandas as pd
# from pandas import DataFrame as df
import numpy as np
from dateutil.parser import parse
from datetime import timedelta as td, datetime as dt
from copy import deepcopy
from _ast import comprehension
from sympy import *
from sympy.parsing.sympy_parser import parse_expr
from collections import defaultdict
from copy import deepcopy
from ilc_matrices import (open_file, extract_criteria,
                          calc_column_sums, normalize_matrix,
                          validate_input, build_score,
                          rtus_ctrl, input_matrix)
 
from volttron.platform.agent import utils, matching, sched
from volttron.platform.messaging import headers as headers_mod, topics
from volttron.platform.agent.utils import jsonapi, setup_logging
 
from volttron.platform.vip.agent import *

MATRIX_ROWSTRING = "%20s\t%12.2f%12.2f%12.2f%12.2f%12.2f"
CRITERIA_LABELSTRING = "\t\t\t%12s%12s%12s%12s%12s"
DATE_FORMAT='%m-%d-%y %H:%M:%S'
setup_logging()
_log = logging.getLogger(__name__)

def ahp(config_path, **kwargs):
    _log = logging.getLogger(__name__)
    logging.basicConfig(level=logging.debug,
                        format='%(asctime)s   %(levelname)-8s %(message)s',
                        datefmt='%m-%d-%y %H:%M:%S')
    config = utils.load_config(config_path)
    location = dict((key, config['device'][key])
                     for key in ['campus', 'building'])
    devices = config['device']['unit']
    agent_id = config.get('agent_id')
    base_device = "devices/{campus}/{building}/".format(**location)
    power_meter = "PowerMeter"
    units = devices.keys()
    devices_topic = (
        base_device + '({})(/.*)?/all$'
        .format('|'.join(re.escape(p) for p in units)))
    bld_pwr_topic = (
        base_device + '({})(/.*)?/all$'
        .format('|'.join(re.escape(p) for p in [power_meter])))
    BUILDING_TOPIC = re.compile(bld_pwr_topic)
    ALL_DEV = re.compile(devices_topic)
    static_config = config['device']
    all_devices = static_config.keys()
    demand_limit = float(config.get("Demand Limit"))
    curtail_time = float(config.get("Curtailment Time", 15.0))

    class AHP(Agent):
        def __init__(self, **kwargs):
            super(AHP, self).__init__(**kwargs)
            self.off_dev = defaultdict(list)
            self.running_ahp = False
            self.builder = defaultdict(dict)
            self.crit_labels = None
            self.row_average = None
            self.failed_control = []
            self.bldg_power = None
            self.transition = False

        @Core.receiver("onstart")
        def starting_base(self, sender, **kwargs):
            excel_file = config.get('excel_file', None)
            
            if self.excel_file is not None:
                self.crit_labels, criteria_arr = extract_criteria(excel_file,
                                                                  "CriteriaMatrix")
                col_sums = calc_column_sums(criteria_arr)
                _, self.row_average = \
                    normalize_matrix(criteria_arr, col_sums)
                print self.crit_labels, criteria_arr
            if not (validate_input(criteria_arr, col_sums, True,
                                   self.crit_labels, CRITERIA_LABELSTRING,
                                   MATRIX_ROWSTRING)):
                _log.info('Inconsistent criteria matrix. Check configuration '
                          'in ahp.xls file')
                # TODO:  MORE USEFULT MESSAGE TO DEAL WITH
                # INCONSISTENT CONFIGURATION
                sys.exit()
            # Setup pubsub to listen to all devices being published.
            driver_prefix = topics.DRIVER_TOPIC_BASE
            _log.debug("subscribing to {}".format(driver_prefix))
            
            self.vip.pubsub.subscribe(peer='pubsub',
                                      prefix=driver_prefix,
                                      callback=self.new_data)
                                      
        def new_data(self, peer, sender, bus, topic, headers, message):
            '''Generate static configuration inputs for priority calculation.
            '''
            if self.transition:
                return
            _log.info('Data Received')
            if BUILDING_TOPIC.match(topic) and not self.running_ahp:
                _log.debug('Reading building power data.')
                self.check_load(headers, message)
            if not ALL_DEV.match(topic):
                return
            if self.running_ahp:
                device = topic.split('/')[3]
            if device not in self.off_dev.keys():
                return
                    
        def query_device(self):
            '''Query Actuator agent for current state of pertinent points on

            curtailable device.
            '''
            for key, value in static_config.items():
                config = static_config[key]
                device = None
                data = {}
                by_mode = deepcopy(config.get('by_mode', None))
                assert by_mode
                for dev, stat in by_mode.items():
                    check_status = self.vip.rpc.call(
                        'platform.actuator', 'get_point',
                        ''.join([location, key, stat])).get(timeout=10)
                    if int(check_status[stat]):
                        device = deepcopy(config[dev])
                        break
                if device is None:
                    self.off_dev.update({key: by_mode.values()})
                    continue
                for point in config['points']:
                    value = self.vip.rpc.call(
                        'platform.actuator', 'get_point',
                        ''.join([location, key, point])).get(timeout=10)
                    data.update({point: value})
                for sub_dev in device:
                    if data[sub_dev]:
                        self.construct_input(key, sub_dev, device[sub_dev], data)
                    else:
                        self.off_dev[key].append(sub_dev)
                        
        def construct_input(self, key, sub_dev, criteria, data):
            '''Declare and construct data matrix for device.'''
            dev_key = ''.join([key, '_', sub_dev])
            self.builder.update({dev_key:{}})
            for item in criteria:
                _name = criteria['name']
                op_type = criteria['operation_type']
                _operation = criteria['operation']
                if isinstance(op_type, str) and op_type == "constant":
                    val = criteria['operation']
                    if val < criteria['minimum']:
                        val = criteria['minimum']
                    if val > criteria['maximum']:
                        val = criteria['maximum']
                    self.builder[dev_key].update({_name: val})
                    continue
                if isinstance(op_type, list) and op_type and op_type[0] == 'mapper':
                    val = config['mapper-' + op_type[1]][_operation]
                    if val < criteria['minimum']:
                        val = criteria['minimum']
                    if val > criteria['maximum']:
                        val = criteria['maximum']
                    self.builder[dev_key].update({_name: val})
                    continue
                if isinstance(op_type, list) and op_type and op_type[0] == 'status':
                    if data[op_type[1]]:
                        val = _operation
                    else:
                        val = 0
                    self.builder[dev_key].update({_name: val})
                    continue
                if isinstance(op_type, list) and op_type and op_type[0][0] == 'staged':
                    val = 0
                    for i in range(1, op_type[0][1]+1):
                        if data[op_type[i][0]]:
                            val += op_type[i][1]
                    if val < criteria['minimum']:
                        val = criteria['minimum']
                    if val > criteria['maximum']:
                        val = criteria['maximum']
                    self.builder[dev_key].update({_name: val})
                    continue
                if isinstance(op_type, list) and op_type and op_type[0] == 'formula':
                    _points = op_type[1].split(" ")
                    points = symbols(op_type[1])
                    expr = parse_expr[_operation]
                    pt_lst =[]
                    for item in _points:
                        pt_lst.append([(item, data[item])])
                    val = expr.subs([pt_lst])
                    if val < criteria['minimum']:
                        val = criteria['minimum']
                    if val > criteria['maximum']:
                        val = criteria['maximum']
                    self.builder[dev_key].update({_name: val})
                    continue
                

        def check_load(self, headers, message):
            '''Check whole building power and if the value is above thenano
 
            the demand limit (demand_limit) then initiate the AHP sequence.
            '''
            obj = jsonapi.loads(message[0])
            bldg_power = float(obj[power_meter])
            if bldg_power > demand_limit:
                self.bldg_power = bldg_power
                self.running_ahp = True
                self.query_device()
                if self.builder is not None:
                    input_arr = input_matrix(self.builder, self.crit_labels)
                if input is not None:
                    scores, score_order = build_score(input_arr, self.row_average)
                ctrl_dev = self.actuator_request(score_order)
                self.curtail(ctrl_dev, scores, score_order)

        def actuator_request(self, score_order):
                now = dt.now()
                str_now = now.strftime(DATE_FORMAT)
                end = now + td(minutes=curtail_time)
                str_end = end.strftime(DATE_FORMAT)
                schedule_request = []
                for dev in score_order:
                    curt_dev = ''.join([base_device, dev])
                    schedule_request = [[curt_dev, str_now, str_end]]
                    result = self.vip.rpc.call('platform.actuator',
                                                'request_new_schedule',
                                                agent_id,              
                                                agent_id,            
                                                'HIGH',                  
                                                schedule_request        
                                                ).get(timeout=10)
                    if result['result'] == 'FAILURE':
                        self.failed_control.append(dev)
                ctrl_dev = [dev for dev in score_order if dev not in self.failed_control]
                return ctrl_dev
        
        def curtail(self, ctrl_dev, scores, score_order):
            dev_keys = self.builder.keys()
            dev_keys = [(item.split('_')[0], item.split('_')[-1]) for item in dev_keys]
            dev_keys = [(item[0], item[-1]) for item in dev_keys if item[0] in ctrl_dev]
            for item in dev_keys:
                pt = static_config[item[0]][item[-1]]
                pt = pt.get('curtail', None)
                if pt is None:
                    _log.error('The "curtail" section of device configuration '
                               'is missing or configured incorrectly')
                curtail_pt = pt.get('point', None)
                curtail_val = pt.get('value', None)
                curtail_load = pt.get('load', None)
                curtail_path = ''.join([location, item[0], curtail_pt])
                result = self.vip.rpc.call('platforlsm.actuator', 'set_point',                        
                                           agent_id, curtail_path,  
                                           curtail_val                                
                                           ).get(timeout=10)

                self.transition == True
                    
                                            

      
    return AHP(**kwargs)               
                    
                    
def main(argv=sys.argv):
    '''Main method called to start the agent.'''  
    utils.vip_main(ahp)


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass                   
                    