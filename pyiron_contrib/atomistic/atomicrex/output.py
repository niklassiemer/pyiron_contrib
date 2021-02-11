from pyiron_base import InputList

class Output(InputList):
    """
    Class to store general output quantities.
    Final properties and function parameter values are stored within
    the respective classes and not here.
    """    
    def __init__(self):
        super().__init__(table_name="output")
        self.error = None
        self.residual = None
        self.iterations = None