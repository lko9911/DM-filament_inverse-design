function Sg = segGen(Ti, Tdef)

% Sg = [material number, start point, end point, start line, end line]
Sg = zeros(1,6);
Sg(1,2) = 1; % initialize as line #1


for i = 1:size(Ti,1)
    match = false;
    for j = 1:size(Tdef,1) 
        if Ti(i,1) == Tdef{j,1}    % Compare Tin and Tdef to find M information of specific T
            Minfo = Tdef{j,2};   
            match = true;
            break;
        end
    end        
    if ~match
        fprintf('No match found in Tdef for material %d\n',Ti(i,1));
        Minfo = zeros(2,1);
    end
    prevLine = 0;
    for k = 1:size(Minfo,1)
        materialNum = Minfo(k,1);
        lineNum = Minfo(k,2);       
        curEnd = Ti(i,3);
        
        if i == 1
            prevEnd = 1;            
        else
            prevEnd = Ti(i-1,3);
        end        
        
        Stemp = zeros(1,6);
        curLine = prevLine + lineNum;
        startLayer = line2layer(prevLine)+1;
        endLayer = line2layer(curLine);
        for ii = startLayer:endLayer
            Stemp(ii,:) = [materialNum, prevEnd, curEnd, ii, layer2line(ii)];
        end
                
% Generate segment in line-by-line manner   
%         Stemp = zeros(1,5);
%         for ii = 1:lineNum
%             curLine = prevLine+ii;
%             Stemp(ii,:) = [materialNum, prevEnd, curEnd, line2layer(curLine), curLine];
%         end      

        prevLine = prevLine+lineNum;
        Sg = [Sg; Stemp];              
    end    
end
Sg( Sg(:,1)==0, :) = [];
Sg = sortrows(Sg, [2 3 4 5]);

end
    
    
