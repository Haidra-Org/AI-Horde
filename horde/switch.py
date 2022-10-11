class Switch:
    active = False

    def activate(self):
        self.active = True
    
    def disable(self):
        self.active = False

    def toggle(self,value):
        self.active = value