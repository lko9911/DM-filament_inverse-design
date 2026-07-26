function Gcode = gcodeWrite(Path, h, fD, printSetting)


prevE = 0;
n = 1;
prevMat = Path{1,3};
mc = 0; % material change 

Gcode = cell(1,1);

curSize = zeros(size(Path,1),4);   %[Xmin Xmax Ymin Ymax]

for i = 1:size(Path,1)
    % printSetting = 1.material num, 2.printF, 3.travelF, 4.retractOn, 5.retractF, 6.retractF(Z), 7.retractE, 8.retractZ
    curMat = Path{i,3};
    
    pset = printSetting( find(printSetting(:,1) == curMat) ,: ) ;
    if isempty(pset)
        error('No printSetting row is defined for material code %d.', curMat);
    end
    printF = pset(2);
    travelF = pset(3);
    retractOn = pset(4);
    retractF = pset(5);
    retractZF = pset(6);
    retractE = pset(7);
    retractZ = pset(8);
    flowRate = pset(9);
    nozzleTemp = pset(10);
    coolingFanOn = pset(11);
    
    Z = Path{i,2}(1);
    w =  Path{i,2}(2);  
    ii = ( 1:1:size( Path{i,1}, 1 ) )';
    X = Path{i,1}(ii,1);
    Y = Path{i,1}(ii,2);   
    dL = sqrt( (X(2:end) - X(1:end-1)).^2 + (Y(2:end) - Y(1:end-1)).^2 );
       
    if Z == h
        printF = printF/2;
        flowRate = flowRate+0.15;
    elseif Z == h*2
        printF = printF/4*3;
    elseif size(Path{i,1}) <= 10
        printF = printF/4;
    end             

    if prevMat ~= curMat
        Gcode{n,1} = 'M107';
        Gcode{n+1,1} = strcat('M104', ' S', num2str(nozzleTemp));
        Gcode{n+2,1} = 'M600';        
        if retractOn == 1
            prevE = prevE - retractE;
        end
        n = n+3;
        mc = mc+1;
    end
    prevMat = curMat;            
    
    E = prevE + cumsum( dL*w*h / (pi/4*fD^2) * flowRate);
    
    Gi{1,1} = strcat( 'G1',' F', num2str(travelF), ' X',num2str(X(1),'%-3.3f'), ' Y',num2str(Y(1),'%-3.3f') );
    Gi{2,1} = strcat( 'G1',' F', num2str(retractZF), ' Z',num2str(Z) );
    Gi{3,1} = strcat( 'G1',' F', num2str(retractF), ' E', num2str(prevE,'%-3.3f') );
    if Z <= h*3 || coolingFanOn == 0
        Gi{4,1} = 'M107';
    else
        Gi{4,1} = 'M106 S64';
    end
        
    G = cellstr(strcat( 'G1',' F', num2str(printF), ' X',num2str(X(2:end),'%-3.3f'), ' Y',num2str(Y(2:end),'%-3.3f'), ' E', num2str(E,'%-3.3f') ) );
    if retractOn == 1
        Go{1,1} = strcat( 'G1',' F', num2str(retractF), ' E', num2str(E(end)-retractE,'%-3.3f') );
        Go{2,1} = strcat( 'G1',' F', num2str(retractZF), ' Z', num2str(Z+retractZ) );
    elseif retractOn == 0
        Go = [];
    end
    
    prevE = E(end);

    Gcode{n,1} = [Gi; G; Go];
    n = n+1;   
    
    curSize(i,:) = [min(X) max(X) min(Y) max(Y)];
    fprintf('Gcode generated: %d / %d \n',i, size(Path,1))
end
hold off

size_total = [min(curSize(:,1)), max(curSize(:,2)), min(curSize(:,3)), max(curSize(:,4))];
fprintf('Material change:  %d times \n', mc)
fprintf('Print size: X %3.3f ~ %3.3f, Y %3.3f ~ %3.3f \n', size_total)

end
