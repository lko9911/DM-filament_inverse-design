This code is developed to generate G-code for printing DM filaments based on user-defined information. 
All the necessary information examples are already included in the source code.

1. System Requirements:
The code comprises the main.m file and other subfunction files. 
It has been tested on MATLAB versions 2019b, 2020b, and 2023a, and does not require any non-standard hardware.

2. Installation Guide:
To install, unzip the file and open the main.m file using MATLAB. 
No installation time is required. 
You may need to update or install the export_fig toolbox, which can be obtained from the following link: 
https://www.mathworks.com/matlabcentral/fileexchange/23629-export_fig
Export_fig is used for visualizing the output (DM filament design), although the resulting G-code can be saved with or without the toolbox.

3. Demo:
To run the code, execute the main.m file using MATLAB. 
The expected output includes a DM filament for printing a 3D object with a color gradient, as displayed in Fig. 1. 
A TXT file containing the G-code will be generated, and the side view of the filament will be displayed.
The extension of the TXT file should be changed to .gcode in order to use it with commercial FDM printers.
The expected run time is less than 3 minutes.

4. Instructions for Use:
User-defined information includes printing parameters, DM filament design, and printing sequence.

© 2023 Sangjoon Ahn, Howon Lee, Kyujin Cho. All Rights Reserved.

